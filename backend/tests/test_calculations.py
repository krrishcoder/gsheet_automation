from decimal import Decimal

from app.services.calculations import ShiftValues, compute_row_update, day_totals, shift_average

D = Decimal


def test_shift_average_rounds_half_up_to_two_places():
    assert shift_average(ShiftValues(21, D("696.380"))) == D("33.16")
    assert shift_average(ShiftValues(42, D("1340.840"))) == D("31.92")
    assert shift_average(ShiftValues(8, D("100.100"))) == D("12.51")  # 12.5125 -> 12.51
    assert shift_average(ShiftValues(4, D("50.02"))) == D("12.51")  # 12.505 -> 12.51 (half up)


def test_average_undefined_cases():
    assert shift_average(ShiftValues(None, D("10"))) is None
    assert shift_average(ShiftValues(5, None)) is None
    assert shift_average(ShiftValues(0, D("10"))) is None
    assert shift_average(ShiftValues(0, D("0"))) == D("0.00")


def test_day_totals_use_all_available_shifts():
    shifts = {1: ShiftValues(21, D("696.380")), 2: ShiftValues(10, D("300.5")), 3: ShiftValues()}
    assert day_totals(shifts) == (D("996.880"), 31)
    assert day_totals({1: ShiftValues(), 2: ShiftValues(), 3: ShiftValues()}) == (None, None)


def test_new_shift_does_not_reset_other_shifts():
    existing = {1: ShiftValues(), 2: ShiftValues(10, D("300.5")), 3: ShiftValues(5, D("150"))}
    comp = compute_row_update(existing, 1, 21, D("696.38"))
    assert comp.shifts_after[2] == existing[2]
    assert comp.shifts_after[3] == existing[3]
    assert comp.day_total_weight == D("1146.880")
    assert comp.day_total_trips == 36
    assert comp.after == ShiftValues(21, D("696.380"))


def test_missing_values_keep_existing_and_zero_overwrites():
    existing = {1: ShiftValues(20, D("650"))}
    nil_qty = compute_row_update(existing, 1, 21, None)
    assert nil_qty.after == ShiftValues(21, D("650"))
    zero = compute_row_update(existing, 1, 0, D("0"))
    assert zero.after == ShiftValues(0, D("0.000"))
    assert zero.average == D("0.00")


def test_correction_replaces_instead_of_adding():
    existing = {1: ShiftValues(21, D("696.380"))}
    comp = compute_row_update(existing, 1, 22, D("700"))
    assert comp.after == ShiftValues(22, D("700.000"))
    assert comp.day_total_trips == 22
    assert comp.changed


def test_identical_values_are_unchanged():
    existing = {1: ShiftValues(21, D("696.38"))}
    assert not compute_row_update(existing, 1, 21, D("696.380")).changed
