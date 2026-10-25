# -*- coding: utf-8 -*-
"""droid_accounts / quota per-account keys, all in temp dirs with fake
keyring files (real AES-GCM, fake JWTs). Nothing real is touched: the
encryption key, the process helpers and the launcher are patched."""
import base64
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import quota                          # noqa: E402
import droid_buddy                    # noqa: E402
import droid_accounts as da           # noqa: E402

from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: E402

KEY = b"k" * 32                       # fake ring key, patched in


def _b64(b):
    return base64.b64encode(b).decode()


def _b64url(obj):
    return base64.urlsafe_b64encode(
        json.dumps(obj).encode()).decode().rstrip("=")


def fake_jwt(email, org, exp=2000000000):
    return "%s.%s.%s" % (_b64url({"alg": "none"}),
                         _b64url({"email": email, "external_org_id": org,
                                  "exp": exp}), _b64url("sig"))


def write_keyring(dirpath, email, org, tag="t"):
    """auth.v2.keyring in Factory's real iv:tag:ct format, plus a marker
    inside the plaintext so file swaps are checkable by content."""
    os.makedirs(dirpath, exist_ok=True)
    plain = {"access_token": fake_jwt(email, org),
             "refresh_token": "rt-" + tag,
             "active_organization_id": org, "whoami": email}
    blob = json.dumps(plain).encode()
    aes = AESGCM(KEY)
    iv = b"i" * 12
    ct = aes.encrypt(iv, blob, None)
    with open(os.path.join(dirpath, "auth.v2.keyring"), "w") as f:
        f.write("%s:%s:%s" % (_b64(iv), _b64(ct[-16:]), _b64(ct[:-16])))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.F = os.path.join(self.tmp.name, "factory")
        self.P = os.path.join(self.tmp.name, "profiles")
        os.makedirs(self.F)
        os.makedirs(self.P)
        da._ident_cache.clear()
        da._pending.clear()
        self.names = os.path.join(self.tmp.name, "droid-accounts.json")
        pts = [mock.patch.object(da, "FACTORY_DIR", self.F),
               mock.patch.object(da, "PROFILES_DIR", self.P),
               mock.patch.object(da, "_enc_key", lambda: KEY),
               mock.patch.object(da, "_droid_procs", lambda: []),
               mock.patch.object(da, "_close_factory", lambda pids=(): None),
               mock.patch.object(da, "_launch_factory", lambda exe: None),
               mock.patch.object(droid_buddy, "_factory_exe",
                                 lambda: r"C:\fake\factory-desktop.exe"),
               mock.patch.object(droid_buddy, "ORG_FILE",
                                 os.path.join(self.F,
                                              "org-managed-settings.cache.json")),
               mock.patch.object(droid_buddy, "SESS_DIR",
                                 os.path.join(self.F, "sessions")),
               mock.patch.object(droid_buddy, "INDEX_FILE",
                                 os.path.join(self.F, "sessions-index.json"))]
        for p in pts:
            p.start()
            self.addCleanup(p.stop)

    def save(self, name, email, org, tag="saved"):
        """A profile written the way 'save' would: live files copied in,
        current.txt set."""
        write_keyring(self.F, email, org, tag)
        da._copy_to_profile(name)
        da._set_current(name)


class TestIdentity(Base):
    def test_parse(self):
        write_keyring(self.F, "a@x.com", "orgA")
        i = da.identity(self.F)
        self.assertEqual(i["email"], "a@x.com")
        self.assertEqual(i["org"], "orgA")
        self.assertEqual(i["exp"], 2000000000)

    def test_missing(self):
        self.assertIsNone(da.identity(self.F))


class TestSync(Base):
    def test_autosave_by_email(self):
        write_keyring(self.F, "new@x.com", "orgN", "live")
        got = da.sync()
        name, email, org, created = got
        self.assertTrue(created)
        self.assertEqual(name, "new@x.com")        # valid profile name
        self.assertRegex(name, da.NAME_RE)
        self.assertEqual(da.get_current(), "new@x.com")
        with open(os.path.join(self.P, "profiles", "new@x.com",
                               "auth.v2.keyring")) as f:
            self.assertIn(":", f.read())

    def test_refresh_matching_profile(self):
        self.save("A", "a@x.com", "orgA", "v1")
        time.sleep(0.02)
        write_keyring(self.F, "a@x.com", "orgA", "v2")   # token refreshed
        got = da.sync()
        self.assertEqual(got[0], "A")
        self.assertFalse(got[3])
        i = da.identity(os.path.join(self.P, "profiles", "A"))
        self.assertEqual(i["email"], "a@x.com")

    def test_skipped_while_busy(self):
        da._busy = True
        try:
            self.assertIsNone(da.sync())
        finally:
            da._busy = False

    def test_no_rewrite_when_unchanged(self):
        """sync() on an unchanged login must not touch the profile files
        or current.txt — Factory/AV can hold them and a pointless rewrite
        is where the PermissionError came from."""
        self.save("A", "a@x.com", "orgA", "a1")
        prof = os.path.join(self.P, "profiles", "A", "auth.v2.keyring")
        cur = os.path.join(self.P, "current.txt")
        time.sleep(0.05)
        pm, cm = os.stat(prof).st_mtime_ns, os.stat(cur).st_mtime_ns
        calls = []
        with mock.patch.object(da, "_replace",
                               side_effect=lambda a, b: calls.append(a)):
            da.sync()
        self.assertEqual(calls, [])
        self.assertEqual(os.stat(prof).st_mtime_ns, pm)
        self.assertEqual(os.stat(cur).st_mtime_ns, cm)


class TestSwitch(Base):
    def test_a_to_b(self):
        self.save("A", "a@x.com", "orgA", "a1")
        self.save("B", "b@x.com", "orgB", "b1")
        # B refreshed its token while live; a stray file too
        write_keyring(self.F, "b@x.com", "orgB", "b2")
        with open(os.path.join(self.F, "auth.v2.file"), "w") as f:
            f.write("STRAY")
        da._ident_cache.clear()
        r = da.switch("A", names_file=self.names)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["email"], "a@x.com")
        self.assertEqual(da.get_current(), "A")
        # live is A now; the stray is gone
        self.assertEqual(os.listdir(self.F), ["auth.v2.keyring"])
        i = da.identity(self.F)
        self.assertEqual(i["org"], "orgA")
        # B's refreshed bytes were folded back into its profile
        pb = os.path.join(self.P, "profiles", "B")
        self.assertTrue(os.path.isfile(os.path.join(pb, "auth.v2.file")))
        # a timestamped backup of what was live exists
        backups = os.listdir(os.path.join(self.P, "backups"))
        self.assertEqual(len(backups), 1)
        self.assertIn("auth.v2.keyring",
                      os.listdir(os.path.join(self.P, "backups", backups[0])))

    def test_cli_running_blocks(self):
        self.save("A", "a@x.com", "orgA")
        self.save("B", "b@x.com", "orgB")
        before = {n: __import__("pathlib").Path(self.F, n).read_bytes()
                  for n in os.listdir(self.F)}
        with mock.patch.object(da, "_droid_procs", lambda: [
                {"pid": 42, "path": r"C:\Users\me\bin\droid.exe"}]):
            r = da.switch("A")
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "cli_running")
        self.assertEqual(r["count"], 1)
        self.assertEqual({n: __import__("pathlib").Path(self.F, n).read_bytes()
                          for n in os.listdir(self.F)}, before)
        # forced: the foreign pid is passed to the closer
        killed = []
        with mock.patch.object(da, "_droid_procs", lambda: [
                {"pid": 42, "path": r"C:\Users\me\bin\droid.exe"}]), \
                mock.patch.object(da, "_close_factory",
                                  lambda pids=(): killed.extend(pids)):
            r = da.switch("A", force=True)
        self.assertTrue(r["ok"], r)
        self.assertIn(42, killed)

    def test_bad_profile(self):
        self.assertEqual(da.switch("a/b")["error"], "bad_name")
        self.assertEqual(da.switch("nope")["error"], "no_profile")


class TestAdd(Base):
    def test_begin_poll_done(self):
        self.save("A", "a@x.com", "orgA", "a1")
        r = da.begin_add()
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["prev_email"], "a@x.com")
        self.assertEqual(os.listdir(self.F), [])
        self.assertIsNone(da.get_current())
        self.assertEqual(da.poll_add(self.names)["state"], "waiting")
        # the new login lands
        write_keyring(self.F, "c@x.com", "orgC", "c1")
        da._ident_cache.clear()
        r = da.poll_add(self.names)
        self.assertEqual(r["state"], "done")
        self.assertEqual(r["email"], "c@x.com")
        self.assertFalse(r["existing"])
        self.assertEqual(da.get_current(), "c@x.com")
        self.assertTrue(os.path.isdir(
            os.path.join(self.P, "profiles", "c@x.com")))

    def test_poll_releases_busy(self):
        """poll_add sets _busy under the lock — it must be released and
        actually take effect (a forgotten `global` left it a no-op)."""
        self.save("A", "a@x.com", "orgA")
        da.begin_add()
        write_keyring(self.F, "c@x.com", "orgC")
        da._ident_cache.clear()
        self.assertEqual(da.poll_add(self.names)["state"], "done")
        self.assertFalse(da._busy)
        self.assertIsNotNone(da.sync())          # not stuck "busy"

    def test_cancel_restores(self):
        self.save("A", "a@x.com", "orgA", "a1")
        da.begin_add()
        r = da.cancel_add(names_file=self.names)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["restored"], "a@x.com")
        self.assertEqual(da.identity(self.F)["org"], "orgA")
        self.assertEqual(da.get_current(), "A")

    def test_cancel_after_login_keeps(self):
        self.save("A", "a@x.com", "orgA", "a1")
        da.begin_add()
        write_keyring(self.F, "d@x.com", "orgD", "d1")
        da._ident_cache.clear()
        r = da.cancel_add(names_file=self.names)
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["kept"])
        self.assertEqual(r["email"], "d@x.com")
        self.assertEqual(da.get_current(), "d@x.com")


class TestList(Base):
    def test_rows(self):
        self.save("A", "a@x.com", "orgA")
        self.save("B", "b@x.com", "orgB")
        rows = da.list_accounts(self.names)
        self.assertEqual(len(rows), 2)
        letters = {r["letter"] for r in rows}
        self.assertEqual(len(letters), 2)
        cur = [r for r in rows if r["current"]]
        self.assertEqual(len(cur), 1)
        self.assertEqual(cur[0]["org"], "orgB")


# ------------------------------------------------------------- quota

class TestQuotaKeys(unittest.TestCase):
    def setUp(self):
        self.creds = {}
        pts = [mock.patch.object(quota, "cred_get",
                                 lambda t=quota.CRED_TARGET: self.creds.get(t)),
               mock.patch.object(quota, "cred_set",
                                 lambda s, t=quota.CRED_TARGET:
                                 self.creds.__setitem__(t, s)),
               mock.patch.object(quota, "factory_org", lambda: "orgA")]
        for p in pts:
            p.start()
            self.addCleanup(p.stop)
        quota._legacy_key.clear()
        self.addCleanup(quota._legacy_key.clear)

    def _call(self, req):
        url = req.full_url
        if url.endswith("/api/cli/whoami"):
            key = req.headers["Authorization"].split()[-1]
            org = {"fk-a": "orgA", "fk-b": "orgB"}.get(key)
            return (200, {"orgId": org}) if org else (401, None)
        if url.endswith("/api/billing/limits"):
            return 200, {"limits": {"standard": {
                "fiveHour": {"usedPercent": 10, "windowEnd": "2030-01-01T00:00:00Z"}}}}
        return 404, None

    def test_set_key_wrong_account(self):
        with mock.patch.object(quota, "_call", self._call):
            r = quota.set_key("orgA", "fk-b")
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "wrong_account")
        self.assertEqual(r["key_org"], "orgB")

    def test_set_key_ok(self):
        with mock.patch.object(quota, "_call", self._call):
            r = quota.set_key("orgA", "fk-a")
        self.assertTrue(r["ok"])
        self.assertEqual(self.creds[quota.key_target("orgA")], "fk-a")

    def test_set_key_bad(self):
        with mock.patch.object(quota, "_call", self._call):
            self.assertEqual(quota.set_key("orgA", "fk-z")["error"], "bad_key")
            self.assertEqual(quota.set_key("orgA", "nope")["error"], "bad_format")

    def test_legacy_migration(self):
        self.creds[quota.CRED_TARGET] = "fk-a"
        with mock.patch.object(quota, "_call", self._call):
            q = quota.fetch_factory()
        self.assertEqual(q["org"], "orgA")
        self.assertEqual(self.creds[quota.key_target("orgA")], "fk-a")
        self.assertEqual(self.creds[quota.CRED_TARGET], "fk-a")   # kept

    def test_no_key(self):
        with mock.patch.object(quota, "_call", self._call):
            with self.assertRaises(RuntimeError) as e:
                quota.fetch_factory()
        self.assertEqual(str(e.exception), "no_key")

    def test_state_hides_other_org(self):
        p = quota.Poller(os.path.join(self.creds_dir(), "c.json"),
                         which=("droid",))
        p.data["droid"] = {"org": "orgB", "windows": [], "at": 1}
        self.assertIsNone(p.state("droid"))      # factory_org = orgA
        p.data["droid"] = {"org": "orgA", "windows": [], "at": 1}
        self.assertIsNotNone(p.state("droid"))

    def test_switch_then_no_key(self):
        """Switched to an account with no key: one poll leaves the entry
        stamped with the *new* org + the error (not the old account's
        numbers), so the card stops hiding and shows the no-key note."""
        p = quota.Poller(os.path.join(self.creds_dir(), "c.json"),
                         which=("droid",))
        p.data["droid"] = {"org": "orgB", "plan": "Standard",
                           "windows": [{"key": "5h", "used": 50}],
                           "at": 1, "error": None, "stale": False}
        with mock.patch.object(quota, "factory_org", lambda: "orgA"), \
                mock.patch.dict(quota.FETCH, {"droid": lambda:
                                              (_ for _ in ()).throw(
                                                  RuntimeError("no_key"))}):
            p.poll_once()
        d = p.data["droid"]
        self.assertEqual(d["org"], "orgA")
        self.assertEqual(d["error"], "no_key")
        self.assertNotIn("windows", d)           # old account's numbers gone
        self.assertIs(p.state("droid"), d)       # shown, not hidden

    def test_state_kicks_refresh_once_per_org(self):
        p = quota.Poller(os.path.join(self.creds_dir(), "c.json"),
                         which=("droid",))
        p.data["droid"] = {"org": "orgB", "windows": [], "at": 1}
        kicks = []
        with mock.patch.object(p, "refresh",
                               side_effect=lambda: kicks.append(1)):
            self.assertIsNone(p.state("droid"))
            self.assertIsNone(p.state("droid"))
            self.assertIsNone(p.state("droid"))
        self.assertEqual(len(kicks), 1)          # once, not every tick

    def creds_dir(self):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, True))
        return d


if __name__ == "__main__":
    unittest.main()
