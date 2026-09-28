import unittest

from ttfix_core import TradingService, market_data_request_fields


class MarketDataRequestTests(unittest.TestCase):
    def setUp(self):
        self.subscription = {
            "request_id": "MD-TEST",
            "symbol": "BZ",
            "exchange": "CME",
            "security_id": "16750313146825052995",
            "security_type": "FUT",
            "maturity": "203307",
            "full_book": True,
            "continuous": True,
        }

    def test_live_request_uses_tt_component_order(self):
        fields = market_data_request_fields(self.subscription, "1")
        self.assertEqual(
            fields,
            [
                ("262", "MD-TEST"), ("263", "1"), ("264", "0"),
                ("265", "1"), ("266", "Y"), ("146", "1"),
                ("55", "BZ"), ("207", "CME"), ("167", "FUT"),
                ("200", "203307"), ("48", "16750313146825052995"),
                ("22", "96"), ("267", "3"), ("269", "0"),
                ("269", "1"), ("269", "2"),
            ],
        )

    def test_snapshot_omits_update_type(self):
        fields = market_data_request_fields(self.subscription, "0")
        self.assertNotIn(("265", "1"), fields)
        self.assertIn(("264", "0"), fields)

    def test_unsubscribe_keeps_original_request_and_instrument(self):
        fields = market_data_request_fields(self.subscription, "2")
        self.assertEqual(fields[:2], [("262", "MD-TEST"), ("263", "2")])
        self.assertIn(("146", "1"), fields)
        self.assertFalse(any(tag in {"264", "265", "266", "267", "269"} for tag, _ in fields))

    def test_failed_send_does_not_leave_phantom_subscription(self):
        config = {
            "mock_mode": False,
            "enable_live_orders": False,
            "market_data": {
                "host": "localhost", "port": 1, "sender_comp_id": "MD",
                "target_comp_id": "TT", "password": "x", "heartbeat": 30,
            },
            "order": {
                "host": "localhost", "port": 1, "sender_comp_id": "OR",
                "target_comp_id": "TT", "password": "x", "heartbeat": 30,
                "account": "test",
            },
        }
        service = TradingService(config)
        try:
            with service.market_session.state_lock:
                service.market_session.state.status = "CONNECTED"
            with self.assertRaises(ConnectionError):
                service.subscribe_market_data("BZ")
            self.assertNotIn("BZ", service.subscriptions)
            self.assertNotIn("BZ", service.quotes)
        finally:
            service.close()

    def test_market_session_and_business_rejects_are_visible(self):
        config = {
            "mock_mode": True,
            "enable_live_orders": False,
            "market_data": {"sender_comp_id": "MD", "target_comp_id": "TT"},
            "order": {"sender_comp_id": "OR", "target_comp_id": "TT", "account": "test"},
        }
        service = TradingService(config)
        events = []
        service.add_listener(lambda event, payload: events.append((event, payload)))
        try:
            service._market_message("", {"35": "j", "372": "V", "380": "2"})
            service._market_message("", {"35": "3", "58": "Tag out of order", "371": "262"})
        finally:
            service.close()
        rejects = [payload for event, payload in events if event == "market_reject"]
        self.assertEqual(rejects[0], "TT business reject: Unknown security")
        self.assertEqual(rejects[1], "TT FIX session reject: Tag out of order (tag 262)")


if __name__ == "__main__":
    unittest.main()
