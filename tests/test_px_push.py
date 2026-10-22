"""Web Push: RFC 8291 encryption, RFC 8292 VAPID, endpoint allowlist, stores, pruning, routes, service worker, manifest."""
import json
import os
import pathlib
import stat

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi.testclient import TestClient

from projectx_desk import push
from projectx_desk.app import create_app
from projectx_desk.hub import SignalRHub
from test_px_app import FakeClient

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATIC = ROOT / "projectx_desk" / "static"
GOOD = "https://fcm.googleapis.com/fcm/send/abc123"
d64 = push.b64u_dec


def pub_of(k):
    return k.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def priv(b64):
    return ec.derive_private_key(int.from_bytes(d64(b64), "big"), ec.SECP256R1())


def make_ua():
    k = ec.generate_private_key(ec.SECP256R1())
    return k, pub_of(k), os.urandom(16)


def ua_decrypt(body, ua_key, ua_pub, auth):
    """Independent receiver side of RFC 8291 section 3.4, written from the RFC, not from push.encrypt."""
    salt, rs, idlen = body[:16], int.from_bytes(body[16:20], "big"), body[20]
    as_pub, ct = body[21:21 + idlen], body[21 + idlen:]
    assert rs >= 18 and len(ct) <= rs
    secret = ua_key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_pub))
    prk = HKDF(hashes.SHA256(), 32, auth, b"WebPush: info\x00" + ua_pub + as_pub).derive(secret)
    cek = HKDF(hashes.SHA256(), 16, salt, b"Content-Encoding: aes128gcm\x00").derive(prk)
    nonce = HKDF(hashes.SHA256(), 12, salt, b"Content-Encoding: nonce\x00").derive(prk)
    pt = AESGCM(cek).decrypt(nonce, ct, None)
    assert pt.endswith(b"\x02")
    return pt[:-1]


def sub_for(ua_pub, auth, endpoint=GOOD):
    return {"endpoint": endpoint, "keys": {"p256dh": push.b64u(ua_pub), "auth": push.b64u(auth)}}


# ---- RFC 8291 appendix A test vector ----------------------------------------------------------------------------------

def test_rfc8291_appendix_a_vector_byte_for_byte():
    as_priv = priv("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw")
    ua_priv = priv("q1dXpw3UpT5VOmu_cf_v6ih07Aems3njxI-JWgLcM94")
    assert push.b64u(pub_of(ua_priv)) == "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"
    body = push.encrypt(b"When I grow up, I want to be a watermelon", pub_of(ua_priv), d64("BTBZMqHH6r4Tts7J_aSIgg"),
                        as_priv, d64("DGv6ra1nlYgDCS1FRnbzlw"))
    assert push.b64u(body) == ("DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_"
                               "yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN")


def test_encrypt_decrypt_round_trip_and_fresh_randomness():
    k, pub, auth = make_ua()
    a, b = push.encrypt(b'{"body":"HALT: new orders refused"}', pub, auth), push.encrypt(b'{"body":"HALT: new orders refused"}', pub, auth)
    assert a != b                                              # fresh salt and ephemeral key every message
    assert ua_decrypt(a, k, pub, auth) == b'{"body":"HALT: new orders refused"}'


def test_encrypt_rejects_bad_keys_and_oversize():
    k, pub, auth = make_ua()
    for args in ((b"x", pub[:64], auth), (b"x", pub, auth[:15]), (b"x", b"\x05" + pub[1:], auth), (b"x" * 5000, pub, auth)):
        with pytest.raises(ValueError):
            push.encrypt(*args)


# ---- RFC 8292 VAPID ---------------------------------------------------------------------------------------------------

def test_vapid_jwt_verifies_has_the_right_claims_and_header(tmp_path):
    v = push.Vapid.load_or_create(tmp_path / "vapid.json")
    auth = v.authorization(GOOD, "mailto:a@b.co", now=1_000_000)
    scheme, rest = auth.split(" ", 1)
    assert scheme == "vapid"
    t = dict(x.strip().split("=", 1) for x in rest.split(","))
    assert t["k"] == v.public_b64 and len(d64(t["k"])) == 65
    claims = push.verify_jwt(t["t"], v.public_b64)
    assert claims == {"aud": "https://fcm.googleapis.com", "exp": 1_000_000 + 12 * 3600, "sub": "mailto:a@b.co"}
    assert json.loads(d64(t["t"].split(".")[0])) == {"typ": "JWT", "alg": "ES256"}
    assert len(d64(t["t"].split(".")[2])) == 64                # raw r||s, not DER
    with pytest.raises(Exception):
        push.verify_jwt(t["t"], push.Vapid(ec.generate_private_key(ec.SECP256R1())).public_b64)


def test_vapid_key_file_is_0600_stable_and_never_served(tmp_path, monkeypatch):
    f = tmp_path / "sub" / "vapid.json"
    v1 = push.Vapid.load_or_create(f)
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert not list(f.parent.glob("*.tmp"))
    v2 = push.Vapid.load_or_create(f)
    assert v1.public_b64 == v2.public_b64
    f.write_text("{not json")                                  # a corrupt file is replaced, not trusted
    assert push.Vapid.load_or_create(f).public_b64 != v1.public_b64


# ---- SSRF allowlist ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "https://fcm.googleapis.com/fcm/send/x", "https://updates.push.services.mozilla.com/wpush/v2/x",
    "https://web.push.apple.com/QAbc", "https://wns2-par02p.notify.windows.com/w/?token=x", "https://fcm.googleapis.com:443/x"])
def test_good_endpoints(url):
    assert push.endpoint_ok(url)


@pytest.mark.parametrize("url", [
    "http://fcm.googleapis.com/x", "https://127.0.0.1/x", "https://localhost/x", "https://169.254.169.254/latest/meta-data",
    "https://[::1]/x", "https://10.0.0.5/x", "https://fcm.googleapis.com.evil.com/x", "https://evilfcm.googleapis.com.attacker.io/x",
    "https://notfcm.googleapis.com/x" if False else "https://xfcm-googleapis.com/x", "https://user:pw@fcm.googleapis.com/x",
    "https://fcm.googleapis.com@evil.com/x", "https://fcm.googleapis.com:8443/x", "https://fcm.googleapis.com/x#frag",
    "https://fcm.googleapis.com/x y", "ftp://fcm.googleapis.com/x", "file:///etc/passwd", "//fcm.googleapis.com/x", "", None, 5,
    "https://fcm.googleapis.com/" + "a" * 700, "https://фcm.googleapis.com/x", "https://push.apple.com.evil/x",
    "https://fcm.googleapis.com\\@evil.com/x", "https://2130706433/x"])
def test_bad_endpoints_are_refused(url):
    assert not push.endpoint_ok(url)


def test_extra_hosts_come_only_from_the_env_list():
    assert not push.endpoint_ok("https://push.example.org/x")
    extra = push.extra_suffixes({"PX_PUSH_HOSTS": "push.example.org, bad host, 10.0.0.1"})
    assert extra == ("push.example.org",)                      # the garbage and the IP literal are dropped
    assert push.endpoint_ok("https://push.example.org/x", extra) and push.endpoint_ok("https://a.push.example.org/x", extra)
    assert not push.endpoint_ok("https://example.org/x", extra)


def test_clean_subscription_checks_key_sizes_and_curve_point():
    k, pub, auth = make_ua()
    assert push.clean_subscription(sub_for(pub, auth))["endpoint"] == GOOD
    off_curve = b"\x04" + b"\x01" * 64
    for bad in (sub_for(pub[:60], auth), sub_for(pub, auth[:8]), sub_for(off_curve, auth), sub_for(pub, auth, "https://evil.com/x"),
                {"endpoint": GOOD}, {"endpoint": GOOD, "keys": "x"}, [], None, {"endpoint": GOOD, "keys": {"p256dh": "!!", "auth": "!!"}}):
        assert push.clean_subscription(bad) is None


# ---- store, prefs, quiet hours ------------------------------------------------------------------------------------------

def test_store_is_0600_atomic_survives_reload_and_caps_devices(tmp_path):
    f = tmp_path / "p" / "subs.json"
    st = push.PushStore(f)
    k, pub, auth = make_ua()
    sid = st.add(push.clean_subscription(sub_for(pub, auth)))
    assert stat.S_IMODE(f.stat().st_mode) == 0o600 and not list(f.parent.glob("*.tmp"))
    assert push.PushStore(f).subs[sid]["prefs"]["events"]["halt"] is True
    assert push.PushStore(f).subs[sid]["prefs"]["events"]["callout"] is False
    for i in range(push.MAX_SUBS - 1):
        st.add(push.clean_subscription(sub_for(pub, auth, f"https://fcm.googleapis.com/x/{i}")))
    with pytest.raises(ValueError):
        st.add(push.clean_subscription(sub_for(pub, auth, "https://fcm.googleapis.com/x/overflow")))
    assert st.add(push.clean_subscription(sub_for(pub, auth))) == sid      # re-subscribing the same device is not a new device
    f.write_text("garbage")
    assert push.PushStore(f).subs == {}


def test_prefs_ignore_unknown_keys_non_bools_and_bad_quiet():
    p = push.clean_prefs({"events": {"halt": False, "callout": True, "evil": True, "fill": "yes", "regime": 1}, "quiet": "22:00-06:30"})
    assert p["events"]["halt"] is False and p["events"]["callout"] is True
    assert p["events"]["fill"] is True and p["events"]["regime"] is False and "evil" not in p["events"]
    assert p["quiet"] == "22:00-06:30"
    assert push.clean_prefs({"quiet": "25:00-06:00"}, p)["quiet"] == "22:00-06:30"       # invalid keeps the old value
    assert push.clean_prefs({"quiet": ""}, p)["quiet"] == ""


def test_quiet_hours_wrap_midnight():
    q = "22:00-06:00"
    assert push.in_quiet(q, 23 * 60) and push.in_quiet(q, 3 * 60) and not push.in_quiet(q, 12 * 60) and not push.in_quiet(q, 6 * 60)
    assert push.in_quiet("09:00-10:00", 9 * 60 + 30) and not push.in_quiet("09:00-10:00", 10 * 60)
    assert not push.in_quiet("", 0) and not push.in_quiet("garbage", 0) and not push.in_quiet("10:00-10:00", 600)


# ---- sending, pruning -------------------------------------------------------------------------------------------------

def mock_client(status, seen=None):
    def h(req):
        if seen is not None:
            seen.append(req)
        return httpx.Response(status)
    return httpx.AsyncClient(transport=httpx.MockTransport(h))


def pusher(tmp_path, status, seen=None, subs=1):
    p = push.Pusher(push.Vapid.load_or_create(tmp_path / "v.json"), push.PushStore(tmp_path / "s.json"), "mailto:a@b.co", mock_client(status, seen))
    uas = []
    for i in range(subs):
        k, pub, auth = make_ua()
        uas.append((k, pub, auth, p.store.add(push.clean_subscription(sub_for(pub, auth, f"https://fcm.googleapis.com/x/{i}")))))
    return p, uas


@pytest.mark.asyncio
async def test_send_posts_an_encrypted_vapid_signed_request(tmp_path):
    seen = []
    p, [(k, pub, auth, sid)] = pusher(tmp_path, 201, seen)
    assert await p.send_to(sid, {"title": "ProjectX Desk", "body": "HALT: new orders refused"}) == "ok"
    r = seen[0]
    assert str(r.url) == "https://fcm.googleapis.com/x/0" and r.headers["content-encoding"] == "aes128gcm"
    assert r.headers["authorization"].startswith("vapid t=") and r.headers["ttl"] and r.headers["urgency"] == "high"
    assert json.loads(ua_decrypt(r.content, k, pub, auth))["body"] == "HALT: new orders refused"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404, 410])
async def test_gone_subscriptions_are_deleted(tmp_path, status):
    p, [(k, pub, auth, sid)] = pusher(tmp_path, status)
    assert await p.send_to(sid, {"body": "x"}) == "gone"
    assert sid not in p.store.subs and sid not in push.PushStore(tmp_path / "s.json").subs      # gone from disk too


@pytest.mark.asyncio
@pytest.mark.parametrize("status,res", [(429, "retry"), (500, "retry"), (503, "retry"), (400, "fail"), (403, "fail"), (200, "ok"), (202, "ok")])
async def test_other_statuses_never_delete(tmp_path, status, res):
    p, [(k, pub, auth, sid)] = pusher(tmp_path, status)
    assert await p.send_to(sid, {"body": "x"}) == res
    assert sid in p.store.subs


@pytest.mark.asyncio
async def test_network_error_is_retry_and_redirects_are_not_followed(tmp_path):
    def boom(req):
        raise httpx.ConnectError("down")
    p, [(k, pub, auth, sid)] = pusher(tmp_path, 201)
    p.client = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    assert await p.send_to(sid, {"body": "x"}) == "retry"
    p.client = mock_client(302)                                # a redirect is a plain failure, never a second request
    assert await p.send_to(sid, {"body": "x"}) == "fail"


@pytest.mark.asyncio
async def test_broadcast_honours_event_toggles_and_quiet_hours(tmp_path):
    seen = []
    p, uas = pusher(tmp_path, 201, seen, subs=2)
    a, b = uas[0][3], uas[1][3]
    p.store.prefs(a, {"events": {"fill": False}})
    p.store.prefs(b, {"quiet": "00:00-23:59"})
    assert await p.broadcast("fill", {"body": "x"}) == 0       # a has fill off, b is in quiet hours
    assert await p.broadcast("fill", {"body": "x"}, critical=True) == 1
    assert await p.broadcast("halt", {"body": "x"}, critical=True) == 2
    assert await p.broadcast("callout", {"body": "x"}) == 0    # off by default everywhere


# ---- routes -------------------------------------------------------------------------------------------------------------

def app_with(tmp_path, status=201):
    fake = FakeClient()
    app = create_app(client=fake, market_hub=SignalRHub(fake, "market", url="ws://127.0.0.1:1/x"),
                     user_hub=SignalRHub(fake, "user", url="ws://127.0.0.1:1/y"), allowed_hosts={"testserver"}, connect=False)
    app.state.pusher = push.Pusher(None, push.PushStore(tmp_path / "s.json"), "mailto:a@b.co", mock_client(status), vapid_path=tmp_path / "v.json")
    return app


def hpost(c, path, tok, body, **hdr):
    h = {"origin": "http://testserver", "x-px-csrf": tok, "content-type": "application/json"}
    h.update(hdr)
    return c.post(path, json=body, headers={k: v for k, v in h.items() if v is not None})


def test_vapid_route_serves_only_the_public_key(tmp_path):
    app = app_with(tmp_path)
    with TestClient(app) as c:
        r = c.get("/api/push/vapid")
        key = r.json()["key"]
        assert len(d64(key)) == 65
        raw = json.loads((tmp_path / "v.json").read_text())
        assert raw["public"] == key and raw["private"] not in r.text


PUSH_POSTS = ["/api/push/subscribe", "/api/push/prefs", "/api/push/unsubscribe", "/api/push/test"]


@pytest.mark.parametrize("path", PUSH_POSTS)
def test_push_posts_use_the_same_gate_as_every_desk_post(tmp_path, path):
    app = app_with(tmp_path)
    with TestClient(app) as c:
        tok = c.get("/api/csrf").json()["token"]
        assert hpost(c, path, tok, {}, origin=None).status_code == 403
        assert hpost(c, path, tok, {}, origin="http://evil.com").status_code == 403
        assert hpost(c, path, tok, {}, **{"x-px-csrf": None}).status_code == 403
        assert hpost(c, path, tok, {}, **{"x-px-csrf": "guess"}).status_code == 403
        assert hpost(c, path, tok, {}, **{"sec-fetch-site": "cross-site"}).status_code == 403
        assert c.post(path, content="a=1", headers={"origin": "http://testserver", "x-px-csrf": tok,
                                                    "content-type": "application/x-www-form-urlencoded"}).status_code == 415
        assert c.get(path).status_code == 405


def test_subscribe_prefs_state_test_unsubscribe_flow(tmp_path):
    app = app_with(tmp_path)
    k, pub, auth = make_ua()
    with TestClient(app) as c:
        tok = c.get("/api/csrf").json()["token"]
        assert hpost(c, "/api/push/subscribe", tok, {"subscription": sub_for(pub, auth, "https://evil.example/x")}).status_code == 400
        assert hpost(c, "/api/push/subscribe", tok, {"subscription": sub_for(pub[:10], auth)}).status_code == 400
        r = hpost(c, "/api/push/subscribe", tok, {"subscription": sub_for(pub, auth), "events": {"callout": True}, "quiet": "22:00-06:00"})
        assert r.status_code == 200
        sid = r.json()["id"]
        assert sid == push.sub_id(GOOD)
        v = c.get("/api/push/state", params={"id": sid}).json()
        assert v["enabled"] and v["events"]["callout"] is True and v["events"]["halt"] is True and v["quiet"] == "22:00-06:00"
        assert GOOD not in json.dumps(v)                                                        # the endpoint is never echoed
        assert hpost(c, "/api/push/prefs", tok, {"id": sid, "events": {"halt": False}}).json()["events"]["halt"] is False
        assert hpost(c, "/api/push/prefs", tok, {"id": "0" * 16, "events": {}}).status_code == 404
        assert hpost(c, "/api/push/test", tok, {"id": sid}).json() == {"result": "ok"}
        assert hpost(c, "/api/push/test", tok, {"id": sid}).status_code == 429                 # rate-limited
        assert hpost(c, "/api/push/unsubscribe", tok, {"id": sid}).json() == {"enabled": False}
        assert c.get("/api/push/state", params={"id": sid}).json()["enabled"] is False
        assert hpost(c, "/api/push/test", tok, {"id": sid}).status_code == 404


def test_test_route_prunes_a_gone_subscription(tmp_path):
    app = app_with(tmp_path, status=410)
    k, pub, auth = make_ua()
    with TestClient(app) as c:
        tok = c.get("/api/csrf").json()["token"]
        sid = hpost(c, "/api/push/subscribe", tok, {"subscription": sub_for(pub, auth)}).json()["id"]
        assert hpost(c, "/api/push/test", tok, {"id": sid}).json() == {"result": "gone"}
        assert c.get("/api/push/state", params={"id": sid}).json()["enabled"] is False


def test_operator_login_covers_push_sw_and_manifest(tmp_path):
    fake = FakeClient()
    tok = __import__("secrets").token_hex(32)
    app = create_app(client=fake, market_hub=SignalRHub(fake, "market", url="ws://127.0.0.1:1/x"),
                     user_hub=SignalRHub(fake, "user", url="ws://127.0.0.1:1/y"), allowed_hosts={"testserver"}, connect=False, operator_token=tok)
    with TestClient(app) as c:
        for p in ("/sw.js", "/static/manifest.webmanifest", "/static/icon-192.png", "/api/push/vapid", "/api/push/state"):
            assert c.get(p).status_code == 401, p
        ok = c.get("/sw.js", auth=("anyone", tok))
        assert ok.status_code == 200
        assert c.get("/static/manifest.webmanifest", auth=("anyone", tok)).status_code == 200


# ---- service worker, manifest, icons ------------------------------------------------------------------------------------

def test_service_worker_route_headers_and_content(tmp_path):
    app = app_with(tmp_path)
    with TestClient(app) as c:
        r = c.get("/sw.js")
        assert r.status_code == 200
        assert r.headers["service-worker-allowed"] == "/" and r.headers["cache-control"] == "no-cache"
        assert "javascript" in r.headers["content-type"]
        src = r.text
        assert "addEventListener('push'" in src and "addEventListener('notificationclick'" in src and "showNotification" in src
        assert "renotify" in src
        assert "addEventListener('fetch'" not in src and "caches." not in src and "cache.put" not in src and "respondWith" not in src
        assert "—" not in src


def test_manifest_meets_install_criteria_and_icons_exist():
    m = json.loads((STATIC / "manifest.webmanifest").read_text())
    assert m["start_url"] == "/" and m["scope"] == "/" and m["id"] and m["display"] == "standalone"
    assert m["name"] and m["short_name"]
    by = {(i["sizes"], i["purpose"]): i for i in m["icons"] if i["type"] == "image/png"}
    assert ("192x192", "any") in by and ("512x512", "any") in by and ("512x512", "maskable") in by
    for i in m["icons"]:
        f = STATIC / pathlib.Path(i["src"]).name
        assert f.exists(), i["src"]
        if i["type"] == "image/png":
            head = f.read_bytes()[:24]
            assert head[:8] == b"\x89PNG\r\n\x1a\n"
            w, h = int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")
            assert f"{w}x{h}" == i["sizes"]
    assert (STATIC / "apple-touch-icon.png").exists()


def test_index_links_manifest_with_credentials_and_keeps_the_byline():
    html = (STATIC / "index.html").read_text()
    assert 'rel="manifest" href="/static/manifest.webmanifest" crossorigin="use-credentials"' in html
    assert 'rel="apple-touch-icon"' in html and 'name="apple-mobile-web-app-capable"' in html
    assert '<a class="byline"' in html and 'id="alerts-btn"' in html and 'src="/static/alerts.js"' in html
    for f in ("alerts.js", "sw.js", "index.html"):
        assert "—" not in (STATIC / f).read_text(), f


def test_no_new_hard_dependency():
    assert "cryptography" not in (ROOT / "requirements.txt").read_text().lower()
    assert "pywebpush" not in (ROOT / "requirements.txt").read_text().lower()
    assert not (ROOT / "requirements-push.txt").exists()


def test_push_modules_have_no_bare_write_and_import_nothing_from_the_order_side():
    import ast
    for name in ("push.py", "notify.py"):
        tree = ast.parse((ROOT / "projectx_desk" / name).read_text())
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom) and n.module:
                assert n.module.split(".")[-1] not in ("guard", "client", "limits"), (name, n.module)
            if isinstance(n, ast.Import):
                assert not any(a.name.endswith((".guard", ".client", ".limits")) for a in n.names), name
            if isinstance(n, ast.Attribute):
                assert n.attr not in ("write", "place", "flatten", "cancel_order", "move_stop", "clear_halt", "reconcile", "trip_halt"), (name, n.attr)
