import unittest

from backend.apps.db.memory import MemoryBackend


class TestMemoryBackend(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.initial = {
            "items": [
                {"id": "a", "name": "alpha", "score": 5, "kind": "x", "nested": {"v": 1}},
                {"id": "b", "name": "beta", "score": 8, "kind": "y"},
                {"id": "c", "name": "gamma", "score": 11, "kind": "x"},
            ]
        }
        self.backend = MemoryBackend(self.initial)
        await self.backend.initialize()

    async def asyncTearDown(self):
        await self.backend.close()

    async def test_lifecycle_and_deep_copy_on_read_and_write(self):
        self.assertTrue(self.backend.is_connected)
        self.assertEqual(self.backend.storage_type, "memory")
        self.assertEqual(self.backend.storage_detail, "in-memory")

        self.initial["items"][0]["nested"]["v"] = 99
        fetched = await self.backend.find_one("items", {"id": "a"})
        self.assertEqual(fetched["nested"]["v"], 1)
        fetched["nested"]["v"] = 42
        self.assertEqual((await self.backend.find_one("items", {"id": "a"}))["nested"]["v"], 1)

        new_document = {"id": "d", "nested": {"v": 2}}
        await self.backend.insert_one("items", new_document)
        new_document["nested"]["v"] = 3
        inserted = await self.backend.find_one("items", {"id": "d"})
        self.assertEqual(inserted["nested"]["v"], 2)

        result = await self.backend.find_many("items")
        result[0]["nested"]["v"] = -1
        self.assertEqual((await self.backend.find_one("items", {"id": "a"}))["nested"]["v"], 1)

        await self.backend.close()
        self.assertFalse(self.backend.is_connected)
        self.assertEqual(await self.backend.count_documents("items"), 4)
        await self.backend.initialize()
        self.assertTrue(self.backend.is_connected)

    async def test_unique_keys_replace_for_trace_span_and_other_collections(self):
        await self.backend.insert_one("traces", {"trace_id": "t1", "name": "first"})
        await self.backend.insert_one("traces", {"trace_id": "t1", "name": "replacement"})
        await self.backend.insert_one("spans", {"span_id": "s1", "name": "first"})
        await self.backend.insert_one("spans", {"span_id": "s1", "name": "replacement"})
        await self.backend.insert_one("other", {"id": 1, "value": "first"})
        await self.backend.insert_one("other", {"id": 1, "value": "replacement"})

        self.assertEqual(await self.backend.count_documents("traces"), 1)
        self.assertEqual(await self.backend.count_documents("spans"), 1)
        self.assertEqual(await self.backend.count_documents("other"), 1)
        self.assertEqual((await self.backend.find_one("traces", {"trace_id": "t1"}))["name"], "replacement")
        self.assertEqual((await self.backend.find_one("spans", {"span_id": "s1"}))["name"], "replacement")
        self.assertEqual((await self.backend.find_one("other", {"id": 1}))["value"], "replacement")

    async def test_filters_comparisons_or_in_ne_sort_limit_and_offset(self):
        self.assertEqual(
            await self.backend.count_documents("items", {"kind": "x", "score": {"$gte": 5, "$lt": 12}}),
            2,
        )
        self.assertEqual(
            await self.backend.count_documents(
                "items",
                {"$or": [{"name": "beta"}, {"score": {"$gt": 10}}]},
            ),
            2,
        )
        self.assertEqual(await self.backend.count("items", {"kind": {"$in": ["y"]}}), 1)
        self.assertEqual(await self.backend.count_documents("items", {"kind": {"$ne": "x"}}), 1)

        descending = await self.backend.find_many("items", sort=[("score", -1)], limit=2)
        self.assertEqual([item["score"] for item in descending], [11, 8])
        ascending = await self.backend.find_many("items", sort=["score"], offset=1, limit=1)
        self.assertEqual([item["score"] for item in ascending], [8])
        self.assertEqual((await self.backend.find_one("items", {"score": {"$lte": 5}}))["id"], "a")

    async def test_update_delete_unknown_collections_and_stats(self):
        self.assertEqual(
            await self.backend.update_one("items", {"id": "b"}, {"score": 9, "updated": True}),
            1,
        )
        self.assertEqual(
            await self.backend.find_one("items", {"id": "b"}),
            {"id": "b", "name": "beta", "score": 9, "kind": "y", "updated": True},
        )
        self.assertEqual(await self.backend.update_one("items", {"id": "missing"}, {"x": 1}), 0)
        self.assertEqual(await self.backend.delete_one("items", {"id": "a"}), 1)
        self.assertEqual(await self.backend.delete_one("items", {"id": "a"}), 0)

        self.assertEqual(await self.backend.count_documents("missing"), 0)
        self.assertEqual(await self.backend.find_many("missing"), [])
        self.assertEqual(await self.backend.find_one("missing", {}), None)
        await self.backend.create_index("items", "score")
        stats = self.backend.get_stats()
        self.assertEqual(stats["storage_type"], "memory")
        self.assertTrue(stats["is_connected"])
        self.assertEqual(stats["collections"]["items"], 2)
        self.assertEqual(stats["total_documents"], 2)


if __name__ == "__main__":
    unittest.main()
