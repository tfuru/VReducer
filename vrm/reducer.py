#!/usr/bin/env python
# -*- coding:utf-8 -*-
import struct
from copy import deepcopy
from io import BytesIO
from itertools import groupby

from PIL import Image

from .cleaner import clean
from .util import find, exists, unique, distance
import pprint

"""
VRoidモデルの削減処理
"""


def unique_vrm_materials(vrm_materials):
    """
    マテリアル名 -> 重複元マテリアル の対応辞書を返す
    :param vrm_materials: VRMマテリアルリスト
    :return: マテリアル名 -> 重複元マテリアル名 の対応辞書
    """
    # ハッシュ化の関係で辞書が使えなかったので、リスト2つで代用
    copied_materials = []  # nameキーを削除したマテリアルのリスト
    unique_material_names = []  # 重複しないマテリアル名リスト
    hair_material_name = ''
    hair_material = None
    for material in vrm_materials:
        copied = deepcopy(material)
        del copied['name']  # 読み込み時に別々になるように書き換えているため、nameキーを除外して比較
        if '_OutlineColor' in copied['vectorProperties']:
            # 0.4.0-p1でOutlineColorが統一されないバグがあるので除外する
            del copied['vectorProperties']['_OutlineColor']

        if copied not in copied_materials:            
            if '_Hair_' not in material['name']:
                copied_materials.append(copied)
                unique_material_names.append(material['name'])
                yield material['name'], unique_material_names[copied_materials.index(copied)]
            else:
                # VRoidMobile 対応 髪マテリアル _HAIR_1 に対応
                if '_HAIR_' not in material['name']:
                    copied_materials.append(copied)                
                    unique_material_names.append(material['name'])
                    hair_material_name = material['name']
                    hair_material = deepcopy(copied)
                    yield material['name'], unique_material_names[copied_materials.index(copied)]
                else:
                    if hair_material_name != '':
                        # VRoidMobile 対応 髪マテリアル _HAIR_1 に対応
                        copied_materials.append(hair_material)
                        unique_material_names.append(hair_material_name)
                        yield material['name'], unique_material_names[copied_materials.index(hair_material)]
                    else:
                        # VRoidStudio 正式版 髪への対応
                        copied_materials.append(copied)
                        unique_material_names.append(material['name'])
                        yield material['name'], unique_material_names[copied_materials.index(copied)]

def deduplicated_materials(gltf):
    """
    重複マテリアルを排除する
    :param gltf: glTFオブジェクト
    :return: 重複排除後のglTFオブジェクト
    """
    gltf = deepcopy(gltf)

    # VRMマテリアルを元に重複排除
    vrm = gltf['extensions']['VRM']
    # マテリアル名 -> 重複元マテリアル名の対応
    unique_name_map = dict(unique_vrm_materials(vrm['materialProperties']))
    unique_material_name_set = unique(unique_name_map.values())

    # マテリアル名 -> マテリアルの対応
    name2materials = {m['name']: m for m in gltf['materials']}
    name2vrm_materials = {m['name']: m for m in vrm['materialProperties']}

    # マテリアルの重複排除
    gltf['materials'] = [name2materials[name] for name in unique_material_name_set]
    vrm['materialProperties'] = [name2vrm_materials[name] for name in unique_material_name_set]

    # プリミティブのマテリアル重複排除
    for mesh in gltf['meshes']:
        for primitive in mesh['primitives']:
            # プリミティブの材質を置換
            material_name = primitive['material']['name']
            # VRoidStudio 正式版への対応
            if material_name in unique_name_map:
                new_name = unique_name_map[material_name]
                primitive['material'] = name2materials[new_name]

    return gltf


def find_meshes(meshes, name):
    """
    指定した名前と部分一致するメッシュを列挙する
    :param meshes: メッシュリスト
    :param name: メッシュ名
    :return: 部分一致したメッシュリスト
    """
    return [mesh for mesh in meshes if name in mesh['name']]


def combine_primitives(primitives):
    """
    プリミティブリストを1つのプリミティブに結合する
    ※連続したプリミティブであることを前提
    :param primitives: プリミティブリスト
    :return: 結合済みプリミティブ、追加アクセッサー、追加bufferView
    """
    # 1つのプリミティブに統合する
    # ヘアメッシュ内のプリミティブインデックスのアクセッサーを列挙
    primitive_indices = [primitive['indices'] for primitive in primitives]

    # プリミティブのbufferView列挙
    buffer_views = list(map(lambda indices: indices['bufferView'], primitive_indices))
    head_view = buffer_views[0]
    # 統合したbufferViewを作成
    buf = head_view['buffer']
    offset = head_view['byteOffset']
    data = b''.join(map(lambda view: view['data'], buffer_views))  # バイトデータ
    new_view = {
        'buffer': buf,
        'byteOffset': offset,
        'byteLength': len(data),
        'target': head_view['target'],
        'data': data
    }
    if 'byteStride' in head_view:
        new_view['byteStride'] = head_view['byteStride']  # NOTE; VRoidでは出力されない

    # アクセッサーを統合
    accessor_count = sum(map(lambda x: x['count'], primitive_indices))  # アクセッサー総要素数
    head_indices = primitive_indices[0]
    new_accessor = {
        'count': accessor_count,
        'byteOffset': head_indices['byteOffset'],
        'bufferView': new_view,
        'componentType': head_indices['componentType'],
        'type': head_indices['type'],
        'normalized': head_indices['normalized']
    }

    # 髪メッシュのプリミティブを統合
    head_primitive = primitives[0]
    new_primitive = {
        'mode': head_primitive['mode'],
        'indices': new_accessor,
        'attributes': head_primitive['attributes'],
        'material': head_primitive['material']
    }

    return new_primitive, new_accessor, new_view


def combine_all_primitives(gltf, name):
    """
    指定したマテリアル名を持つプリミティブ結合
    :param gltf: gltfオブジェクト
    :param name: マテリアル名
    :return: プリミティブ結合後のgltfオブジェクト
    """
    gltf = deepcopy(gltf)
    # ヘアメッシュ
    hair_meshes = find_meshes(gltf['meshes'], name)
    if not hair_meshes:
        return gltf
    hair_mesh = hair_meshes[0]

    # マテリアルごとにプリミティブをまとめる
    grouped_primitives = groupby(hair_mesh['primitives'], key=lambda p: p['material'])

    new_primitives = []
    for material, primitives in grouped_primitives:
        # 1つのプリミティブに統合する
        primitive, accessor, buffer_view = combine_primitives(list(primitives))
        new_primitives.append(primitive)
        gltf['accessors'].append(accessor)
        gltf['bufferViews'].append(buffer_view)

    hair_mesh['primitives'] = new_primitives

    return gltf


def remove_primitives(gltf, material_names):
    """
    指定したマテリアル名のプリミティブを削除する
    :param gltf: glTFオブジェクト
    :param material_names: マテリアル名リスト
    :return: プリミティブ削除後のglTFオブジェクト
    """
    gltf = deepcopy(gltf)

    def contain_name(name):
        for material_name in material_names:
            if name in material_name:
                return True  # マテリアル名と部分一致
        return False

    # プリミティブ削除
    for mesh in gltf['meshes']:
        mesh['primitives'] = filter(lambda p: not contain_name(p['material']['name']), mesh['primitives'])
    return gltf


def shrink_gltf_materials(materials):
    """
    バンプマップ、スフィアマップを削除する
    :param materials: glTFマテリアルリスト
    """
    for material in materials:
        for tex_name in ['emissiveTexture', 'normalTexture']:
            if tex_name in material:
                del material[tex_name]


def shrink_vrm_materials(vrm_materials):
    """
    バンプマップ、スフィアマップを削除する
    :param vrm_materials: VRMマテリアルリスト
    """
    for material in vrm_materials:
        # バンプマップ、スフィアマップ、アウトラインテクスチャ削除
        unused = ['_BumpMap', '_SphereAdd', '_OutlineWidthTexture']
        material['textureProperties'] = {k: v for k, v in material['textureProperties'].items() if k not in unused}

        # 法線マップ無効化
        remove_options = ['_NORMALMAP']
        material['keywordMap'] = {k: v for k, v in material['keywordMap'].items() if k not in remove_options}


def shrink_materials(gltf):
    """
    バンプマップ、スフィアマップを削除する
    :param gltf: glTFオブジェクト
    """
    gltf = deepcopy(gltf)
    shrink_gltf_materials(gltf['materials'])
    shrink_vrm_materials(gltf['extensions']['VRM']['materialProperties'])
    return gltf


def emissive_mtoon_material(material):
    """
    EmissiveTextureで表示するように変更
    :param material: VRMマテリアル
    """
    # GI Intensity -> 0
    material['floatProperties']['_IndirectLightIntensity'] = 0
    # Emissive Color -> Color
    vec_props = material['vectorProperties']
    vec_props['_EmissionColor'] = vec_props['_Color']
    # Color -> (0,0,0,1), Shade Color -> (0,0,0,1)
    vec_props['_Color'] = vec_props['_ShadeColor'] = [0, 0, 0, 1]
    # Emissive Texture -> MainTexture
    material['textureProperties']['_EmissionMap'] = material['textureProperties']['_MainTex']


def emissive_mtoon_materials(gltf):
    """
    Clusterの一部会場でMToonモデルが黒くなる問題への対策
    https://clusterhelp.zendesk.com/hc/ja/articles/360021584012-cluster-v1-6-14-2019-1-8-
    :param gltf: glTFオブジェクト
    :return:
    """
    gltf = deepcopy(gltf)
    for material in gltf['extensions']['VRM']['materialProperties']:
        emissive_mtoon_material(material)
    return gltf


def sorted_primitives(primitives, material_name_order):
    """
    指定したマテリアル順にプリミティブをソートする
    :param primitives: プリミティブリスト
    :param material_name_order: マテリアル部分名(描画順)
    :return: ソートしたプリミティブリスト
    """
    max_weight = len(material_name_order) + 1
    material_name_order = filter(None, material_name_order)

    def weight(name):
        # マテリアル名の重みを返す
        for n, order_name in enumerate(material_name_order):
            if order_name in name:
                return n
        return max_weight

    return sorted(primitives, key=lambda p: weight(p['material']['name']))


def sorted_mesh_primitives(gltf, mesh_name, material_name_order):
    """
    :param gltf: glTFオブジェクト
    :param mesh_name: メッシュ部分名
    :param material_name_order: 描画順のマテリアル部分名
    :return: マテリアル順にプリミティブをソートしたglTFオブジェクト
    """
    gltf = deepcopy(gltf)
    for mesh in find_meshes(gltf['meshes'], mesh_name):
        mesh['primitives'] = sorted_primitives(mesh['primitives'], material_name_order)

    return gltf


def find_material_from_name(materials, name):
    """
    :param materials: マテリアルリスト
    :param name: 検索マテリアル名
    :return: マテリアル名に部分一致するマテリアルを返す。見つからなければNone
    """
    return find(lambda m: name and name in m['name'], materials)


def find_material(gltf, name):
    """
    :param gltf: glTFオブジェクト
    :param name: 検索マテリアル名
    :return: マテリアル名に部分一致するglTFマテリアルを返す。見つからなければNone
    """
    return find_material_from_name(gltf['materials'], name)


def find_near_vrm_material(gltf, name, material):
    """
    :param gltf: glTFオブジェクト
    :param name: 検索マテリアル名
    :param material: 比較対象のVRMマテリアル
    :return: 色の近いマテリアル名に部分一致するglTFマテリアルを返す。見つからなければNone
    """
    # 比較対象のマテリアルを除く
    vrm_materials = gltf['extensions']['VRM']['materialProperties']
    materials = [m for m in vrm_materials if name and name in m['name'] and m != material]
    if not materials:
        return None

    def color_distance(m1, m2):
        v1, v2 = m1['vectorProperties'], m2['vectorProperties']
        return distance(v1['_Color'], v2['_Color'])

    # 最も色の近いマテリアルを返す
    return min(materials, key=lambda m: color_distance(m, material))


def find_vrm_material(gltf, name):
    """
    :param gltf: glTFオブジェクト
    :param name: 検索マテリアル名
    :return: マテリアル名に部分一致するVRMマテリアルを返す。見つからなければNone
    """
    return find_material_from_name(gltf['extensions']['VRM']['materialProperties'], name)


def load_img(image_buffer):
    """
    画像ファイル(バイトデータ)をPILで読み込む
    :param image_buffer: 画像ファイルのバイトデータ
    :return: PIL.Imageオブジェクト
    """
    return Image.open(BytesIO(image_buffer))


def image2bytes(img, fmt):
    """
    PIL.Imageオブジェクトを指定フォーマットの画像ファイル(バイトデータ)に変換する
    :param img: PIL.Imageオブジェクト
    :param fmt: 画像ファイルフォーマット
    :return: 画像ファイル(バイトデータ)
    """
    with BytesIO() as bio:
        img.save(bio, format=fmt)
        return bio.getvalue()


def max_size(resize_info):
    """
    リサイズ情報から結合先として必要な画像サイズを計算して返す
    :param resize_info: リサイズ情報
    :return: width, height
    """
    max_w, max_h = 0, 0
    for name, info in resize_info.items():
        pos = info['pos']
        size = info['size']
        max_w = max(max_w, pos[0] + size[0])
        max_h = max(max_h, pos[1] + size[1])
    return max_w, max_h


def primitives_has_material(gltf, material_name):
    """
    指定したマテリアル名に部分一致するプリミティブを列挙する
    :param gltf: glTFオブジェクト
    :param material_name: マテリアル名
    :return: プリミティブリスト(generator)
    """
    for mesh in gltf['meshes']:
        for primitive in mesh['primitives']:
            if material_name and material_name in primitive['material']['name']:
                yield primitive


def list_primitives(gltf, names):
    """
    指定したマテリアル名を持つプリミティブの情報を列挙する
    マテリアル名、プリミティブ、bufferViewインデックスを列挙
    :param gltf: glTFオブジェクト
    :param names: マテリアル名リスト
    :return: (マテリアル名、プリミティブ、bufferViewインデックス)リスト(generator)
    """
    for name in names:
        for primitive in primitives_has_material(gltf, name):
            view_index = gltf['bufferViews'].index(primitive['attributes']['TEXCOORD_0']['bufferView'])
            yield (name, primitive, view_index)


def combine_material(gltf, resize_info, base_material_name, texture_size=(2048, 2048)):
    """
    再配置情報で指定されたマテリアルを結合する
    テクスチャも結合する
    :param gltf: glTFオブジェクト
    :param resize_info: マテリアル名とテクスチャ配置情報
    :param base_material_name: 統合先にするマテリアル
    :param texture_size: 指定したサイズ以下に縮小する
    :return: マテリアル結合したglTFオブジェクト
    """
    no_base_materials = [find_vrm_material(gltf, name) for name in resize_info if base_material_name != name]
    if not no_base_materials:
        return gltf  # 結合先でないマテリアルがない場合、結合済み

    gltf = deepcopy(gltf)

    vrm_materials = {name: find_vrm_material(gltf, name) for name in resize_info}
    main_tex_sources = {name: material['textureProperties']['_MainTex']['source'] for name, material in
                        vrm_materials.items() if material}

    # リサイズ指定サイズ
    resize_w, resize_h = max_size(resize_info)
    # テクスチャ指定サイズ
    tex_w, tex_h = texture_size

    # 指定テクスチャサイズ以下になるようにスケール係数を計算
    image_w, image_h = (min(resize_w, tex_w), min(resize_h, tex_h))
    scale_w, scale_h = (image_w / float(resize_w), image_h / float(resize_h))

    def scaled():
        # スケールを適用したリサイズ情報を返す
        for name, info in resize_info.items():
            x, y = info['pos']
            pos = (int(x * scale_w), int(y * scale_h))
            w, h = info['size']
            size = (int(w * scale_w), int(h * scale_h))
            yield name, {'pos': pos, 'size': size}

    scaled_info = dict(scaled())

    # 再配置情報を元に1つの画像にまとめる
    one_image = Image.new("RGBA", (image_w, image_h), (0, 0, 0, 0))
    for name, tex_source in main_tex_sources.items():
        pil_image = load_img(tex_source['bufferView']['data'])
        info = scaled_info[name]
        resized = pil_image.resize(info['size'], Image.BICUBIC)  # 透過境界部分にノイズが出ないようにBICUBICを使用
        one_image.paste(resized, info['pos'])
    new_view = {'data': image2bytes(one_image, 'png')}  # pngファイルデータに変換
    # 結合画像名は各画像名を結合した名前にする
    image_names = [source['name'] for source in main_tex_sources.values() if source['name']]
    new_image = {'name': '-'.join(image_names), 'mimeType': 'image/png', 'bufferView': new_view}
    # one_image.show()

    # テクスチャ更新
    vrm_material = find_vrm_material(gltf, base_material_name)
    texture = vrm_material['textureProperties']['_MainTex']
    texture['source'] = new_image
    gltf['images'].append(new_image)
    gltf['bufferViews'].append(new_view)

    # マテリアル統一(テクスチャを更新しているので適用するだけで良い)
    # VRM上の拡張マテリアルはclean処理で削除される
    new_material = find_material(gltf, base_material_name)

    width, height = map(float, (resize_w, resize_h))

    def uv_scale(name):
        # スケール率計算
        paste_info = resize_info[name]
        paste_x, paste_y = paste_info['pos']
        paste_w, paste_h = paste_info['size']
        x, y = (paste_x / width, paste_y / height)
        w, h = (paste_w / width, paste_h / height)
        return x, y, w, h

    # オリジナルバッファー
    original_view_datas = {}
    for _, _, view_index in list_primitives(gltf, resize_info.keys()):
        if view_index not in original_view_datas:
            original_view_datas[view_index] = deepcopy(gltf['bufferViews'][view_index]['data'])

    for name, primitive, view_index in list_primitives(gltf, resize_info.keys()):
        # マテリアル更新
        primitive['material'] = new_material
        # 頂点インデックス一覧
        accessor = primitive['indices']
        indices_buffer = accessor['bufferView']['data']
        indices_offset = accessor['byteOffset']
        indices = map(lambda i: struct.unpack_from('I', indices_buffer, indices_offset + i * 4)[0],
                      range(accessor['count']))

        # uvバッファ
        original_data = original_view_datas[view_index]
        uv_accessor = primitive['attributes']['TEXCOORD_0']
        uv_view = uv_accessor['bufferView']
        uv_data = uv_view['data']

        # スケール率計算
        x, y, w, h = uv_scale(name)
        for index in set(indices):
            uv_offset = index * 8
            ou, ov = struct.unpack_from('2f', original_data, uv_offset)
            u, v = struct.unpack_from('2f', uv_data, uv_offset)
            if ou != u or ov != v:
                continue  # 更新されていればスキップ
            u, v = (x + u * w, y + v * h)
            uv_data = uv_data[:uv_offset] + struct.pack('2f', u, v) + uv_data[uv_offset + 8:]
        uv_view['data'] = uv_data  # 更新

    return gltf


def reduced_image(image_buffer, texture_size):
    """
    画像を指定サイズ以下に縮小する
    :param image_buffer: イメージファイルバイトデータ
    :param texture_size: 画像の縮小上限値
    :return: 新しいイメージファイルバイトデータ
    """
    pil_image = load_img(image_buffer)
    w, h = pil_image.size
    max_w, max_h = texture_size
    if w <= max_w and h <= max_h:
        return image_buffer

    width, height = min(w, max_w), min(h, max_h)
    new_image = pil_image.resize((width, height), Image.BICUBIC)

    return image2bytes(new_image, 'png')


def reduced_images(gltf, texture_size):
    """
    画像を指定サイズ以下に縮小する
    :param gltf: glTFオブジェクト
    :param texture_size: テクスチャサイズ
    :return: 画像リサイズ後のglTFオブジェクト
    """
    gltf = deepcopy(gltf)
    for image in gltf['images']:
        buffer_view = image['bufferView']
        buffer_view['data'] = reduced_image(buffer_view['data'], texture_size)
    return gltf


def replace_shade(gltf):
    """
    陰部分の色を光の当たる部分と同色にする(陰色の無視)
    :param gltf: glTFオブジェクト
    :return: 編集護のglTFオブジェクト
    """
    gltf = deepcopy(gltf)
    for material in gltf['extensions']['VRM']['materialProperties']:
        vec_props = material['vectorProperties']
        vec_props['_ShadeColor'] = vec_props['_Color']
    return gltf


"""
VRoidモデルの服装識別子
"""
CLOTH_NAKED = 'BIG_BOSS'
CLOTH_STUDENT = 'STUDENT'
CLOTH_ONE_PIECE = 'ONE_PIECE'
CLOTH_MALE_STUDENT = 'MALE_STUDENT'


def get_cloth_type(gltf):
    """
    マテリアル情報から服装を判定する
    :param gltf: glTFオブジェクト
    :return: 服装識別子
    """
    names = map(lambda x: x['name'], gltf['materials'])
    for name in names:
        if name.startswith('F00_001'):
            return CLOTH_STUDENT
        elif name.startswith('F00_002'):
            return CLOTH_ONE_PIECE
        elif name.startswith('F00_003') or name.startswith('M00_001'):
            return CLOTH_MALE_STUDENT
    return CLOTH_NAKED


def find_eye_extra_name(gltf):
    """
    > < 目のマテリアル名を取得する
    :param gltf: glTFオブジェクト
    :return: > < 目マテリアル名
    """
    material_names = [m['name'] for m in gltf['materials']]

    def contain_extra_eye(name):
        candidates = ['_EyeExtra_', '_FaceEyeSP']
        return exists(lambda c: c in name, candidates)

    return find(contain_extra_eye, material_names)


def reduce_vroid(gltf, replace_shade_color, texture_size, emissive):
    """
    VRoidモデルを軽量化する
    :param gltf: glTFオブジェクト(VRM拡張を含む)
    :param replace_shade_color: Trueで陰色を消す
    :param texture_size: テクスチャサイズの上限値
    :param emissive: TrueでEmissiveテクスチャで表示(光源の影響を無視する)
    :return: 軽量化したglTFオブジェクト
    """
    # マテリアルの重複排除
    gltf = deduplicated_materials(gltf)

    # 髪プリミティブ統合
    print( 'combine hair primitives...' )
    gltf = combine_all_primitives(gltf, 'Hair')

    # バンプマップ、スフィアマップを削除
    print( 'shrink materials...' )
    gltf = shrink_materials(gltf)

    # 顔のプリミティブ描画順を並び替え
    print( 'sort face primitives...' )
    gltf = sorted_mesh_primitives(gltf, 'Face', ['_Face_', find_eye_extra_name(gltf), '_FaceMouth_',
                                                 '_FaceEyeline_', '_FaceEyelash_', '_FaceBrow_',
                                                 '_EyeWhite_', '_EyeIris_', '_EyeHighlight_'])

    # マテリアルを結合
    print( 'combine materials...' )

    cloth_type = get_cloth_type(gltf)

    if cloth_type == CLOTH_STUDENT:
        # 制服上下、リボン、靴
        gltf = combine_material(gltf, {
            'baseColorTexture': {'pos': (0, 0), 'size': (2048, 1536)},
            'baseColorTexture': {'pos': (0, 1536), 'size': (512, 512)},
            'baseColorTexture': {'pos': (512, 1536), 'size': (512, 512)},
            'baseColorTexture': {'pos': (1024, 1536), 'size': (512, 512)}
        }, 'baseColorTexture', texture_size)

    elif cloth_type == CLOTH_MALE_STUDENT:
        gltf = combine_material(gltf, {
            'baseColorTexture': {'pos': (0, 0), 'size': (2048, 1024)},
            'baseColorTexture': {'pos': (0, 1024), 'size': (1024, 1024)},
            'baseColorTexture': {'pos': (1024, 1024), 'size': (512, 512)},
            'baseColorTexture': {'pos': (1024, 1536), 'size': (512, 512)}
        }, 'baseColorTexture', texture_size)

    elif cloth_type == CLOTH_ONE_PIECE:
        # ワンピース、靴
        # 0.3.0: F00_002_Onepiece
        # 0.4.0-p1: F00_002_Onepice
        # 0.6.3: F00_002_01_Onepice
        gltf = combine_material(gltf, {
            'baseColorTexture': {'pos': (0, 0), 'size': (2048, 1536)},
            'baseColorTexture': {'pos': (0, 1536), 'size': (512, 512)}
        }, 'baseColorTexture', texture_size)

    # 体、顔、口
    gltf = combine_material(gltf, {
        '_Face_': {'pos': (0, 0), 'size': (512, 512)},
        '_FaceMouth_': {'pos': (512, 0), 'size': (512, 512)},
        '_Body_': {'pos': (0, 512), 'size': (2048, 1536)}
    }, '_Face_', texture_size)
    # レンダータイプを変更
    face_mat = find_vrm_material(gltf, '_Face_')
    face_mat['keywordMap']['_ALPHATEST_ON'] = True
    face_mat['tagMap']["RenderType"] = 'TransparentCutout'

    # アイライン、まつ毛
    resize_info = {
        find_eye_extra_name(gltf): {'pos': (0, 0), 'size': (1024, 512)},
        '_FaceEyeline_': {'pos': (0, 512), 'size': (1024, 512)},
        '_FaceEyelash_': {'pos': (0, 1024), 'size': (1024, 512)}
    }
    # VRoidStudio 正式版 _FaceBrow_
    face_brow_material = find_vrm_material(gltf, '_FaceBrow_')
    if face_brow_material:
        resize_info[face_brow_material['name']] = {'pos': (0, 1536), 'size': (1024, 512)}
    gltf = combine_material(gltf, resize_info, '_FaceEyeline_', texture_size)

    # VRoidMobile対応 アイライン、まつ毛 レンダータイプを変更
    eye_line = find_vrm_material(gltf, '_FaceEyeline_')
    if eye_line:
        eye_line['keywordMap']['_ALPHATEST_ON'] = True
        eye_line['tagMap']["RenderType"] = 'TransparentCutout'
        eye_line['floatProperties']["_BlendMode"] = 1
        eye_line['floatProperties']["_CullMode"] = 0
        eye_line['floatProperties']["_ZWrite"] = 1
        eye_line['renderQueue'] = 2450

    # 瞳孔、ハイライト、白目
    gltf = combine_material(gltf, {
        '_EyeIris_': {'pos': (0, 0), 'size': (1024, 512)},
        '_EyeHighlight_': {'pos': (0, 512), 'size': (1024, 512)},
        '_EyeWhite_': {'pos': (0, 1024), 'size': (1024, 512)}
    }, '_EyeHighlight_', texture_size)
    
    # VRoidMobile対応 目が光る事を修正
    eye_highlight = find_vrm_material(gltf, '_EyeHighlight_')
    if eye_highlight:
        eye_highlight['tagMap']["RenderType"] = 'TransparentCutout'
        eye_highlight['floatProperties']["_CullMode"] = 0
        eye_highlight['floatProperties']["_BlendMode"] = 1
        eye_highlight['floatProperties']["_ZWrite"] = 1
        eye_highlight['keywordMap']['_ALPHATEST_ON'] = True
        eye_highlight['renderQueue'] = 2450

    # VRoidStudio 正式版 めがね _GlassesLowFrame_
    glasses_material = find_vrm_material(gltf, '_GlassesLowFrame_')
    if glasses_material:
        resize_info = {
            glasses_material['name']:{'pos': (0, 0), 'size': (1024, 512)}
        }
        glasses_low_lens_material = find_vrm_material(gltf, '_GlassesLowLens_')
        if glasses_low_lens_material:
            resize_info[glasses_low_lens_material['name']] = {'pos': (0, 512), 'size': (1024, 512)}
        gltf = combine_material(gltf, resize_info, '_FaceEyeline_', texture_size)

    # 髪の毛、頭の下毛
    hair_back_material = find_vrm_material(gltf, '_HairBack_')
    if hair_back_material:
        # VRoidMobile対応 髪テクスチャ 配置変更
        # hair_resize = {'_HairBack_': {'pos': (512, 0), 'size': (1024, 1024)}}
        hair_resize = {'_HairBack_': {'pos': (0, 0), 'size': (1024, 512)}}
        hair_material = find_near_vrm_material(gltf, '_Hair_', hair_back_material)
        if hair_material:
            # VRoidMobile対応 髪テクスチャ 配置変更
            # hair_resize[hair_material['name']] = {'pos': (0, 0), 'size': (512, 1024)}
            hair_resize[hair_material['name']] = {'pos': (0, 512), 'size': (1024, 512)}
            gltf = combine_material(gltf, hair_resize, hair_material['name'], texture_size)

    # VRoidMobile対応 首アクセサリ を Tops とまとめる
    tops_material = find_vrm_material(gltf, '_Tops_')
    if tops_material:
        accessory_material = find_vrm_material(gltf, '_Accessory_')
        if accessory_material:
            resize_info = {
                '_Tops_': {'pos': (0, 0), 'size': (512, 512)},
                '_Accessory_': {'pos': (0, 512), 'size': (512, 512)}
            }
            gltf = combine_material(gltf, resize_info, tops_material['name'], texture_size)

    # VRoidStudio 正式版 髪の毛 F00_000_Hair_00_HAIR_01 - F00_000_Hair_00_HAIR_06
    hair_01_material = find_vrm_material(gltf, '_Hair_00_HAIR_01')
    hair_04_material = find_vrm_material(gltf, '_Hair_00_HAIR_04')
    hair_06_material = find_vrm_material(gltf, '_Hair_00_HAIR_06')
    if hair_01_material and (hair_04_material or hair_06_material):
        # texture_size 2048, 2048
        hair_resize = {
            hair_01_material['name']: {'pos': (0, 0), 'size': (1024, 341)},
        }
        hair_02_material = find_vrm_material(gltf, '_Hair_00_HAIR_02')
        if hair_02_material:
            hair_resize[hair_02_material['name']] = {'pos': (0, 341), 'size': (1024, 341)}
        hair_05_material = find_vrm_material(gltf, '_Hair_00_HAIR_05')
        if hair_05_material:
            hair_resize[hair_05_material['name']] = {'pos': (0, 682), 'size': (1024, 341)}
        hair_06_material = find_vrm_material(gltf, '_Hair_00_HAIR_06')
        if hair_06_material:
            hair_resize[hair_06_material['name']] = {'pos': (0, 1024), 'size': (1024, 341)}
        hair_03_material = find_vrm_material(gltf, '_Hair_00_HAIR_03')
        if hair_03_material:
            hair_resize[hair_03_material['name']] = {'pos': (0, 1365), 'size': (1024, 341)}
        hair_04_material = find_vrm_material(gltf, '_Hair_00_HAIR_04')
        if hair_04_material:
            hair_resize[hair_04_material['name']] = {'pos': (0, 1706), 'size': (1024, 341)}
        gltf = combine_material(gltf, hair_resize, hair_01_material['name'], texture_size)
    
    # VRoidStudio 正式版 髪プリミティブ統合
    gltf = combine_all_primitives(gltf, 'Hair')

    if replace_shade_color:
        # 陰色を消す
        gltf = replace_shade(gltf)

    if emissive:
        # Emissiveテクスチャで表示、光源を無視する
        gltf = emissive_mtoon_materials(gltf)

    # 不要要素削除
    gltf = clean(gltf)

    # 他のテクスチャ画像サイズの変換
    print( 'reduced images...' )
    gltf = reduced_images(gltf, texture_size)

    return clean(gltf)
