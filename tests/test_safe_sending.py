import asyncio
import os
import tempfile
import unittest
from unittest import mock

import store
import tg
from telethon.errors import FloodWaitError


class ContactCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "tool.db")
        self.db_patch = mock.patch.object(store, "DB_PATH", self.db_path)
        self.data_patch = mock.patch.object(store, "DATA_DIR", self.temp_dir.name)
        self.db_patch.start()
        self.data_patch.start()
        store.init_db()
        store.upsert_account("+8613800138000", "session")
        self.account_id = store.get_account_by_phone("+8613800138000")["id"]

    def tearDown(self):
        self.data_patch.stop()
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def test_cache_upsert_preserves_profile_name_when_refresh_has_no_name(self):
        store.upsert_contact_cache(
            self.account_id,
            [
                {
                    "phone_digits": "8613912345678",
                    "user_id": 42,
                    "access_hash": 99,
                    "first_name": "张三",
                    "last_name": "张",
                    "is_registered": True,
                }
            ],
        )
        store.upsert_contact_cache(
            self.account_id,
            [
                {
                    "phone_digits": "8613912345678",
                    "user_id": 42,
                    "access_hash": 100,
                    "is_registered": True,
                }
            ],
        )

        cached = store.get_contact_cache(self.account_id, ["8613912345678"])

        self.assertEqual(cached["8613912345678"]["first_name"], "张三")
        self.assertEqual(cached["8613912345678"]["access_hash"], "100")

    def test_deleting_account_also_deletes_contact_cache(self):
        store.upsert_contact_cache(
            self.account_id,
            [{"phone_digits": "8613912345678", "is_registered": False}],
        )

        store.delete_account(self.account_id)

        self.assertEqual(
            store.get_contact_cache(self.account_id, ["8613912345678"]), {}
        )


class FakeClient:
    def __init__(self, send_results=None):
        self.send_results = list(send_results or [])
        self.send_count = 0
        self.disconnected = False

    async def send_file(self, peer, media):
        self.send_count += 1
        if self.send_results:
            result = self.send_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return mock.Mock(id=self.send_count)

    async def disconnect(self):
        self.disconnected = True


class SafeSendingTests(unittest.IsolatedAsyncioTestCase):
    def make_task(self, total):
        return {
            "id": "task-test",
            "account_id": 1,
            "status": "running",
            "total": total,
            "done": 0,
            "ok": 0,
            "failed": 0,
            "skipped": 0,
            "logs": [],
            "stop": asyncio.Event(),
            "error": None,
            "waiting_reason": None,
            "wait_until": None,
        }

    async def test_same_account_cannot_start_a_second_task(self):
        tg._active_account_tasks[7] = "task-existing"
        tg._tasks["task-existing"] = {"status": "waiting"}
        try:
            with mock.patch.object(
                store,
                "get_account",
                return_value={"id": 7, "phone": "+8613800138000", "session": "session"},
            ):
                with self.assertRaisesRegex(ValueError, "已有发送任务"):
                    await tg.start_share(
                        7,
                        [{"type": "chat", "id": 123}],
                        "+8613911111111",
                        {},
                    )
        finally:
            tg._active_account_tasks.pop(7, None)
            tg._tasks.pop("task-existing", None)

    async def test_batch_pause_happens_after_configured_message_count(self):
        client = FakeClient()
        task = self.make_task(3)
        waits = []

        async def no_sleep(seconds, stop_event):
            return None

        async def record_wait(current_task, seconds, reason):
            waits.append((seconds, reason))
            return True

        with (
            mock.patch.object(tg, "_client", return_value=client),
            mock.patch.object(tg, "_connect_or_stop", return_value=True),
            mock.patch.object(tg, "_prepare_contacts", return_value=({}, {})),
            mock.patch.object(tg, "_sleep_or_stop", side_effect=no_sleep),
            mock.patch.object(tg, "_wait_with_status", side_effect=record_wait),
        ):
            await tg._run_share(
                task,
                {"id": 1, "phone": "+8613800138000", "session": "session"},
                [{"type": "chat", "id": 123, "name": "测试群"}],
                tg.parse_numbers("+8613911111111\n+8613922222222\n+8613933333333"),
                {
                    "interval": 1,
                    "batch_size": 2,
                    "batch_pause": 300,
                    "allow_empty_name": True,
                },
            )

        self.assertEqual(task["status"], "done")
        self.assertEqual(task["ok"], 3)
        self.assertEqual(task["done"], 3)
        self.assertEqual(waits, [(300, "批次冷却")])
        self.assertTrue(client.disconnected)

    async def test_fresh_contact_cache_skips_telegram_import(self):
        parsed = tg.parse_numbers("+8613911111111")
        task = self.make_task(1)
        fresh_cache = {
            "8613911111111": {
                "phone_digits": "8613911111111",
                "user_id": 42,
                "access_hash": "99",
                "first_name": "张三",
                "last_name": "",
                "is_registered": True,
                "checked_at": int(tg.time.time()),
            }
        }

        with (
            mock.patch.object(store, "get_contact_cache", return_value=fresh_cache),
            mock.patch.object(tg, "_import_contacts") as import_contacts,
        ):
            users, names = await tg._prepare_contacts(mock.Mock(), task, 1, parsed)

        import_contacts.assert_not_called()
        self.assertEqual(users, {"8613911111111": (42, 99)})
        self.assertEqual(names, {"8613911111111": ("张三", "")})

    async def test_profile_lookup_restores_contact_before_propagating_flood_wait(self):
        class ProfileClient:
            def __init__(self):
                self.requests = []

            async def __call__(self, request):
                self.requests.append(type(request).__name__)
                if len(self.requests) == 1:
                    raise FloodWaitError(request=request, capture=30)
                return mock.Mock()

        client = ProfileClient()
        task = self.make_task(1)

        with self.assertRaises(FloodWaitError):
            await tg._fetch_profile_names(
                client,
                task,
                1,
                {"8613911111111": (42, 99)},
            )

        self.assertEqual(
            client.requests,
            ["DeleteContactsRequest", "ImportContactsRequest"],
        )

    async def test_short_flood_wait_retries_current_card_without_advancing_progress(self):
        flood = FloodWaitError(request=None, capture=10)
        client = FakeClient([flood, mock.Mock(id=88)])
        task = self.make_task(1)
        waits = []

        async def no_sleep(seconds, stop_event):
            return None

        async def record_wait(current_task, seconds, reason):
            waits.append((seconds, reason))
            current_task["status"] = "running"
            return True

        with (
            mock.patch.object(tg, "_client", return_value=client),
            mock.patch.object(tg, "_connect_or_stop", return_value=True),
            mock.patch.object(tg, "_prepare_contacts", return_value=({}, {})),
            mock.patch.object(tg, "_sleep_or_stop", side_effect=no_sleep),
            mock.patch.object(tg, "_wait_with_status", side_effect=record_wait),
        ):
            await tg._run_share(
                task,
                {"id": 1, "phone": "+8613800138000", "session": "session"},
                [{"type": "chat", "id": 123, "name": "测试群"}],
                tg.parse_numbers("+8613911111111"),
                {"interval": 1, "batch_size": 20, "batch_pause": 300},
            )

        self.assertEqual(client.send_count, 2)
        self.assertEqual(task["ok"], 1)
        self.assertEqual(task["failed"], 0)
        self.assertEqual(task["done"], 1)
        self.assertEqual(
            waits,
            [(15, f"Telegram 限流：发送 {tg.mask_phone('+8613911111111')}")],
        )

    async def test_long_flood_wait_stops_without_retrying(self):
        flood = FloodWaitError(request=None, capture=tg.MAX_AUTO_FLOOD_WAIT + 1)
        client = FakeClient([flood])
        task = self.make_task(1)

        with (
            mock.patch.object(tg, "_client", return_value=client),
            mock.patch.object(tg, "_connect_or_stop", return_value=True),
            mock.patch.object(tg, "_prepare_contacts", return_value=({}, {})),
            mock.patch.object(tg, "_sleep_or_stop", return_value=None),
            mock.patch.object(tg, "_wait_with_status") as wait_with_status,
        ):
            await tg._run_share(
                task,
                {"id": 1, "phone": "+8613800138000", "session": "session"},
                [{"type": "chat", "id": 123, "name": "测试群"}],
                tg.parse_numbers("+8613911111111"),
                {"interval": 1, "batch_size": 20, "batch_pause": 300},
            )

        wait_with_status.assert_not_awaited()
        self.assertEqual(client.send_count, 1)
        self.assertEqual(task["status"], "stopped")
        self.assertEqual(task["failed"], 1)
        self.assertEqual(task["done"], 1)
        self.assertIn("账号熔断", task["error"])


if __name__ == "__main__":
    unittest.main()
