import asyncio
import os
import tempfile
import unittest
from unittest import mock

import store
import tg
from telethon.errors import FloodWaitError, SlowModeWaitError


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

    def test_send_guard_and_rate_limit_events_are_persistent(self):
        store.set_account_cooldown(self.account_id, 123456, "FLOOD_WAIT:sendMedia")
        store.record_rate_limit_event(
            self.account_id,
            "messages.sendMedia",
            "FLOOD_WAIT",
            wait_seconds=30,
            target_key="chat:7",
        )
        store.block_account_sending(self.account_id, "PEER_FLOOD")

        guard = store.get_account_send_guard(self.account_id)
        events = store.list_rate_limit_events(self.account_id)

        self.assertTrue(guard["blocked"])
        self.assertEqual(guard["reason"], "PEER_FLOOD")
        self.assertEqual(events[0]["method"], "messages.sendMedia")
        self.assertEqual(events[0]["target_key"], "chat:7")

        store.clear_account_send_block(self.account_id)
        self.assertFalse(store.get_account_send_guard(self.account_id)["blocked"])


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
            with (
                mock.patch.object(
                    store,
                    "get_account",
                    return_value={
                        "id": 7,
                        "phone": "+8613800138000",
                        "session": "session",
                    },
                ),
                mock.patch.object(
                    store,
                    "get_account_send_guard",
                    return_value={"blocked": False, "cooldown_until": None},
                ),
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

    async def test_persistent_account_guard_blocks_new_tasks(self):
        with (
            mock.patch.object(
                store,
                "get_account",
                return_value={"id": 7, "phone": "+8613800138000", "session": "session"},
            ),
            mock.patch.object(
                store,
                "get_account_send_guard",
                return_value={
                    "blocked": True,
                    "reason": "PEER_FLOOD",
                    "cooldown_until": None,
                },
            ),
        ):
            with self.assertRaisesRegex(ValueError, "@SpamBot"):
                await tg.start_share(
                    7,
                    [{"type": "chat", "id": 123}],
                    "+8613911111111",
                    {},
                )

    async def test_active_flood_cooldown_blocks_new_tasks(self):
        with (
            mock.patch.object(
                store,
                "get_account",
                return_value={"id": 7, "phone": "+8613800138000", "session": "session"},
            ),
            mock.patch.object(
                store,
                "get_account_send_guard",
                return_value={
                    "blocked": False,
                    "reason": "FLOOD_WAIT:messages.sendMedia",
                    "cooldown_until": int(tg.time.time()) + 120,
                },
            ),
        ):
            with self.assertRaisesRegex(ValueError, "冷却期"):
                await tg.start_share(
                    7,
                    [{"type": "chat", "id": 123}],
                    "+8613911111111",
                    {},
                )

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
            mock.patch.object(
                tg, "_prepare_contacts", return_value=({}, {})
            ) as prepare_contacts,
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
        prepare_contacts.assert_not_awaited()

    async def test_fresh_contact_cache_skips_phone_resolution(self):
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
            mock.patch.object(tg, "_resolve_contacts") as resolve_contacts,
        ):
            users, names = await tg._prepare_contacts(mock.Mock(), task, 1, parsed)

        resolve_contacts.assert_not_awaited()
        self.assertEqual(users, {"8613911111111": (42, 99)})
        self.assertEqual(names, {"8613911111111": ("张三", "")})

    async def test_resolve_phone_is_debounced_and_caches_profile_names(self):
        class ResolveClient:
            def __init__(self):
                self.requests = []

            async def __call__(self, request):
                self.requests.append(type(request).__name__)
                user_id = 40 + len(self.requests)
                return mock.Mock(
                    peer=mock.Mock(user_id=user_id),
                    users=[
                        mock.Mock(
                            id=user_id,
                            access_hash=90 + user_id,
                            first_name=f"用户{user_id}",
                            last_name="",
                        )
                    ],
                )

        client = ResolveClient()
        task = self.make_task(2)
        waits = []

        async def record_sleep(seconds, stop_event):
            waits.append(seconds)

        with (
            mock.patch.object(tg, "_sleep_or_stop", side_effect=record_sleep),
            mock.patch.object(store, "upsert_contact_cache") as cache_upsert,
        ):
            users, names = await tg._resolve_contacts(
                client,
                task,
                1,
                tg.parse_numbers("+8613911111111\n+8613922222222"),
            )

        self.assertEqual(
            client.requests,
            ["ResolvePhoneRequest", "ResolvePhoneRequest"],
        )
        self.assertEqual(len(waits), 1)
        self.assertGreater(waits[0], 2.9)
        self.assertEqual(set(users), {"8613911111111", "8613922222222"})
        self.assertEqual(names["8613911111111"], ("用户41", ""))
        self.assertEqual(cache_upsert.call_count, 2)

    async def test_lookup_runs_only_when_registration_filter_is_enabled(self):
        client = FakeClient()
        task = self.make_task(1)
        parsed = tg.parse_numbers("+8613911111111;张三")

        async def no_sleep(seconds, stop_event):
            return None

        with (
            mock.patch.object(tg, "_client", return_value=client),
            mock.patch.object(tg, "_connect_or_stop", return_value=True),
            mock.patch.object(
                tg,
                "_prepare_contacts",
                return_value=({"8613911111111": (42, 99)}, {}),
            ) as prepare_contacts,
            mock.patch.object(tg, "_sleep_or_stop", side_effect=no_sleep),
        ):
            await tg._run_share(
                task,
                {"id": 1, "phone": "+8613800138000", "session": "session"},
                [{"type": "chat", "id": 123, "name": "测试群"}],
                parsed,
                {
                    "interval": 1,
                    "batch_size": 20,
                    "batch_pause": 0,
                    "skip_unresolved": True,
                },
            )

        prepare_contacts.assert_awaited_once()
        self.assertEqual(task["ok"], 1)

    async def test_slow_mode_defers_only_the_affected_target(self):
        class PerTargetClient(FakeClient):
            def __init__(self):
                super().__init__()
                self.calls = []
                self.first_chat_attempt = True

            async def send_file(self, peer, media):
                self.calls.append(peer.chat_id)
                if peer.chat_id == 123 and self.first_chat_attempt:
                    self.first_chat_attempt = False
                    raise SlowModeWaitError(request=None, capture=1)
                return mock.Mock(id=len(self.calls))

        client = PerTargetClient()
        task = self.make_task(2)
        clock = [100.0]

        async def no_sleep(seconds, stop_event):
            return None

        async def advance_wait(current_task, seconds, reason):
            clock[0] += seconds
            return True

        with (
            mock.patch.object(tg, "_client", return_value=client),
            mock.patch.object(tg, "_connect_or_stop", return_value=True),
            mock.patch.object(tg, "_sleep_or_stop", side_effect=no_sleep),
            mock.patch.object(tg, "_wait_with_status", side_effect=advance_wait),
            mock.patch.object(tg, "_monotonic", side_effect=lambda: clock[0]),
            mock.patch.object(store, "record_rate_limit_event"),
        ):
            await tg._run_share(
                task,
                {"id": 1, "phone": "+8613800138000", "session": "session"},
                [
                    {"type": "chat", "id": 123, "name": "慢速群"},
                    {"type": "chat", "id": 456, "name": "正常群"},
                ],
                tg.parse_numbers("+8613911111111;张三"),
                {"interval": 1, "batch_size": 20, "batch_pause": 0},
            )

        self.assertEqual(client.calls, [123, 456, 123])
        self.assertEqual(task["ok"], 2)
        self.assertEqual(task["done"], 2)

    async def test_peer_flood_persists_account_guard(self):
        class PeerFloodError(Exception):
            pass

        client = FakeClient([PeerFloodError("PEER_FLOOD")])
        task = self.make_task(1)

        with (
            mock.patch.object(tg, "_client", return_value=client),
            mock.patch.object(tg, "_connect_or_stop", return_value=True),
            mock.patch.object(tg, "_sleep_or_stop", return_value=None),
            mock.patch.object(store, "record_rate_limit_event"),
            mock.patch.object(store, "block_account_sending") as block_sending,
        ):
            await tg._run_share(
                task,
                {"id": 1, "phone": "+8613800138000", "session": "session"},
                [{"type": "chat", "id": 123, "name": "测试群"}],
                tg.parse_numbers("+8613911111111;张三"),
                {"interval": 1, "batch_size": 20, "batch_pause": 0},
            )

        block_sending.assert_called_once_with(1, "PeerFloodError")
        self.assertEqual(task["status"], "stopped")
        self.assertIn("持久化熔断", task["error"])

    async def test_short_flood_wait_retries_current_card_without_advancing_progress(
        self,
    ):
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
        self.assertIn("账号冷却时间", task["error"])


if __name__ == "__main__":
    unittest.main()
