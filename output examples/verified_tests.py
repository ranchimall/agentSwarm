def test_case_1():
    assert second_largest_unique([1, 2, 3, 4]) == 3

def test_case_2():
    assert second_largest_unique([5, 5, 5, 5]) is None

def test_case_3():
    assert second_largest_unique([10]) is None

def test_case_4():
    assert second_largest_unique([2, 2, 1, 1, 3, 3]) == 2

def test_case_5():
    assert second_largest_unique([7, 7, 8, 9, 8, 6]) == 8

def test_case_6():
    assert second_largest_unique([]) is None
