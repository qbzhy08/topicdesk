"""自建领域标签：新建 / 打标签 / 删除 / 重启不覆盖 / 关键词判断 / 选源。"""
from app import db, domains, server
from app.collect import sources
from tests.test_collect import CollectBase


def cfg(sid):
    return db.query_one("SELECT config FROM sources WHERE id=?", (sid,))["config"] or {}


class DomainTagTest(CollectBase):
    def setUp(self):
        super().setUp()
        self.c = server.app.test_client()

    def test_builtin_new_tags(self):
        ids = domains.ids()
        for d in ("auto", "health", "game", "consumer"):
            self.assertIn(d, ids)
        self.assertIn("auto", sources.guess_domains("固态电池量产进展"))

    def test_create_default_sources_and_guess(self):
        r = self.c.post("/api/domains", json={"name": "宠物", "hints": "猫粮，狗粮、宠物医院"})
        self.assertEqual(r.status_code, 201, r.json)
        d = r.json
        self.assertEqual(set(d["tagged_sources"]), {"zhihu", "xiaohongshu", "douyin", "bilibili"})
        self.assertIn(d["id"], cfg("zhihu")["domains"])
        self.assertTrue(cfg("zhihu")["domains_custom"])
        self.assertIn(d["id"], sources.guess_domains("哪款猫粮适合幼猫"))
        self.assertIn(d["id"], domains.prompt_list())
        lst = {x["id"]: x for x in self.c.get("/api/domains").json}
        self.assertEqual(lst[d["id"]]["sources"], 4)
        # 重启（重新 seed）不覆盖用户的标签
        sources.seed_sources()
        self.assertIn(d["id"], cfg("zhihu")["domains"])

    def test_explicit_sources_and_dup(self):
        r = self.c.post("/api/domains", json={"name": "户外运动", "sources": ["ithome"]})
        self.assertEqual(r.json["tagged_sources"], ["ithome"])
        self.assertNotIn(r.json["id"], cfg("zhihu").get("domains", []))
        self.assertEqual(self.c.post("/api/domains", json={"name": "户外运动"}).status_code, 400)
        self.assertEqual(self.c.post("/api/domains", json={"name": "车"}).status_code, 400)

    def test_delete_strips_sources(self):
        did = self.c.post("/api/domains", json={"name": "留学"}).json["id"]
        out = self.c.delete(f"/api/domains/{did}").json
        self.assertEqual(out["sources_updated"], 4)
        self.assertNotIn(did, cfg("zhihu")["domains"])
        self.assertNotIn(did, domains.ids())
        self.assertEqual(self.c.delete("/api/domains/tech").status_code, 400)
        self.assertEqual(self.c.delete("/api/domains/c_nope").status_code, 404)

    def test_patch_source_domains(self):
        r = self.c.patch("/api/sources/ithome", json={"domains": ["health", "bogus"]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(cfg("ithome")["domains"], ["health"])
        self.assertEqual(self.c.patch("/api/sources/ithome", json={"domains": ["bogus"]}).status_code, 400)
        sources.seed_sources()
        self.assertEqual(cfg("ithome")["domains"], ["health"])

    def test_any_domain_sources(self):
        self.assertTrue(cfg("web_search").get("any_domain"))
        self.assertTrue(cfg("wechat").get("any_domain"))
