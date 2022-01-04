#!/usr/bin/env python

from math import sqrt


def unique(seq):
    """
    :param seq: リスト
    :return: 重複のないリストを返す
    """
    new_list = []
    for x in seq:
        if x not in new_list:
            new_list.append(x)
    return new_list


def find(func, seq):
    """
    :param func: 条件
    :param seq: リスト
    :return: 条件を満たす最初の要素を返す、見つからなければNone
    """
    for x in seq:
        if func(x):
            return x
    return None


def exists(func, seq):
    """
    :param func: 条件
    :param seq: リスト
    :return: 条件を満たす要素があればTrue、見つからなければNone
    """
    for x in seq:
        if func(x):
            return True
    return None


def distance(vec1, vec2):
    """
    2つのベクトルの距離を計算する
    :param vec1: ベクトル1
    :param vec2: ベクトル2
    :return: ユークリッド距離を返す
    """
    diff_seq = [s1 - s2 for s1, s2 in zip(vec1, vec2)]
    sq_ds = sum([ds * ds for ds in diff_seq])
    return sqrt(sq_ds)
