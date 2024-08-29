import datetime as dt
import unittest

from connectlife.appliance import convert


class TestAppliance(unittest.TestCase):

    def test_convert_int(self):
        self.assertEqual(1, convert("1"))
        self.assertEqual(0, convert("0"))
        self.assertEqual(-1, convert("-1"))

    def test_convert_float(self):
        self.assertEqual(0.67, convert(0.67))

    def test_convert_datetime(self):
        self.assertEqual(
            dt.datetime(2024, 9, 12, 21, 25, 33, tzinfo=dt.UTC),
            convert("2024/09/12T21:25:33")
        )
        self.assertEqual(
            dt.datetime(2, 11, 30, 00, 00, 00, tzinfo=dt.UTC),
            convert("0002/11/30T00:00:00")
        )
        self.assertEqual(
            dt.datetime(dt.MAXYEAR, 12, 31, 23, 59, 59, tzinfo=dt.UTC),
            convert("16679/02/18T23:47:45")
        )

    def test_convert_str(self):
        self.assertEqual("string", convert("string"))
