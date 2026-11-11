import datetime as dt
import unittest

from connectlife.appliance import ConnectLifeAppliance, convert


class TestAppliance(unittest.TestCase):

    def test_convert_int(self):
        self.assertEqual(1, convert("1"))
        self.assertEqual(0, convert("0"))
        self.assertEqual(-1, convert("-1"))

    def test_convert_float(self):
        self.assertEqual(0.67, convert(0.67))
        self.assertEqual(23.5, convert("23.5"))
        self.assertEqual(-1.5, convert("-1.5"))
        self.assertEqual(0.0, convert("0.0"))

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

    def test_convert_already_converted(self):
        """convert() should be idempotent for all output types."""
        self.assertEqual(42, convert(42))
        self.assertEqual(0.67, convert(0.67))
        d = dt.datetime(2024, 9, 12, 21, 25, 33, tzinfo=dt.UTC)
        self.assertEqual(d, convert(d))


class TestApplianceUpdates(unittest.TestCase):

    def _appliance(self) -> ConnectLifeAppliance:
        return ConnectLifeAppliance(None, {
            "wifiId": "w", "deviceId": "d", "puid": "p", "deviceNickName": "n", "deviceFeatureCode": "400",
            "deviceFeatureName": "f", "deviceTypeCode": "007", "deviceTypeName": "t", "role": 1, "roomId": 1,
            "roomName": "r", "offlineState": 1, "seq": 1, "bindTime": None, "useTime": None, "createTime": None,
            "statusList": {"t_power": "0", "t_temp": "20"},
        })

    def test_update_status_merges_converted(self):
        appliance = self._appliance()
        self.assertEqual(appliance.update_status({"t_power": "1", "t_new": "x"}), {"t_power", "t_new"})
        self.assertEqual(appliance.status_list, {"t_power": 1, "t_temp": 20, "t_new": "x"})

    def test_update_status_unchanged(self):
        appliance = self._appliance()
        self.assertEqual(appliance.update_status({"t_power": "0", "t_temp": "20"}), set())

    def test_update_online(self):
        appliance = self._appliance()
        self.assertFalse(appliance.update_online(True))
        self.assertTrue(appliance.update_online(False))
        self.assertEqual(appliance.offline_state, 0)
