"""The claude backend: one streamed Claude API request per lecture.

Most of these fake the SDK, so they run without it installed and without a
key. They pin the decisions the backend makes: what is sent, what is written,
and what is refused before any money is spent. The last test uses the real
SDK against a local server to pin the request as it actually leaves the
machine, because a fake cannot notice an SDK upgrade that stops sending
`fallbacks`.
"""

import http.server
import json
import sys
import threading
import types

import pytest

NS = types.SimpleNamespace


class _APIError(Exception):
    def __init__(self, message="rejected"):
        super().__init__(message)
        self.message = message


def reply(text="# Notes\n\nAt 12:58 he said the exam covers this.",
          stop="end_turn", model="claude-opus-5", category=None, extra=()):
    blocks = [NS(type="thinking", thinking="reasoning that is not notes"),
              *extra, NS(type="text", text=text)]
    return NS(content=blocks, stop_reason=stop, model=model,
              stop_details=NS(category=category) if stop == "refusal" else None,
              usage=NS(input_tokens=40_000, output_tokens=8_000))


@pytest.fixture
def sdk(monkeypatch):
    """Install a fake `anthropic` module; returns the list of stream() calls."""
    def install(message=None, raises=None):
        calls = []

        class Stream:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def get_final_message(self):
                return message if message is not None else reply()

        class Client:
            def __init__(self, **kwargs):
                self.beta = NS(messages=NS(stream=self._stream))

            def _stream(self, **kwargs):
                calls.append(kwargs)
                if raises is not None:
                    raise raises
                return Stream()

        mod = NS(Anthropic=Client,
                 AuthenticationError=type("AuthenticationError", (_APIError,), {}),
                 NotFoundError=type("NotFoundError", (_APIError,), {}),
                 BadRequestError=type("BadRequestError", (_APIError,), {}))
        monkeypatch.setitem(sys.modules, "anthropic", mod)
        return calls
    return install


def claude(cfg, **kw):
    kw.setdefault("model", "claude-opus-5")
    return cfg(backend="claude", **kw)


def bundle_with(n_slides, chars_per_slide=3000):
    out = ["# Lecture", ""]
    for i in range(n_slides):
        out += [f"**Slide text @ {i}:{i % 60:02d}:**",
                f"> slide {i} coefficient 5.{i:02d} " + "x" * chars_per_slide, ""]
    out += [f"[{j}:00] the lecturer explains point {j}" for j in range(40)]
    return "\n".join(out)


# ----------------------------------------------------------------- what is sent

def test_the_request_uses_adaptive_thinking_and_the_configured_effort(ls, cfg, lecture, sdk):
    calls = sdk()
    d, _ = lecture(bundle=bundle_with(3))

    ls.run_analysis(claude(cfg, effort="medium"), d, use_slides=True,
                    prompt=ls.DELTA_PROMPT)

    sent = calls[0]
    assert sent["thinking"] == {"type": "adaptive"}
    assert sent["output_config"] == {"effort": "medium"}
    assert sent["max_tokens"] == ls.CLAUDE_MAX_TOKENS
    text = sent["messages"][0]["content"][-1]["text"]
    assert text.startswith(ls.DELTA_PROMPT) and "slide 2 coefficient" in text


def test_opus_5_is_sent_with_server_side_fallbacks(ls, cfg, lecture, sdk):
    calls = sdk()
    d, _ = lecture(bundle=bundle_with(1))
    ls.run_analysis(claude(cfg), d, use_slides=True, prompt=ls.DELTA_PROMPT)
    assert calls[0]["fallbacks"] == "default"
    assert calls[0]["betas"] == ["server-side-fallback-2026-07-01"]


def test_models_outside_the_documented_families_get_no_fallbacks(ls, cfg, lecture, sdk):
    calls = sdk(message=reply(model="claude-sonnet-5"))
    d, _ = lecture(bundle=bundle_with(1))
    ls.run_analysis(claude(cfg, model="claude-sonnet-5"), d, use_slides=True,
                    prompt=ls.DELTA_PROMPT)
    assert "fallbacks" not in calls[0] and "betas" not in calls[0]


def test_slide_images_go_before_the_text(ls, cfg, lecture, sdk, tmp_path):
    calls = sdk()
    img = tmp_path / "slide.jpg"
    img.write_bytes(b"\xff\xd8\xff\xe0not really a jpeg")
    ls.claude_analysis(claude(cfg), tmp_path, "BODY", "PROMPT", [img, img])
    content = calls[0]["messages"][0]["content"]
    assert [c["type"] for c in content] == ["image", "image", "text"]
    assert content[0]["source"]["media_type"] == "image/jpeg"


def test_a_bundle_too_big_for_the_local_window_goes_to_claude_whole(ls, cfg, lecture, sdk):
    """The reason to use this backend at all. At the local 32k window this
    bundle loses slides; on Claude every slide arrives and no omission note
    is added."""
    calls = sdk()
    body = bundle_with(40)
    trimmed, note, _ = ls.fit_to_context(body, 32_768)
    assert "omitted" in note, "the bundle must be big enough to be trimmed locally"
    d, _ = lecture(bundle=body)

    ls.run_analysis(claude(cfg), d, use_slides=True, prompt=ls.DELTA_PROMPT)

    text = calls[0]["messages"][0]["content"][-1]["text"]
    assert all(f"slide {i} coefficient" in text for i in range(40))
    assert "omitted" not in text


# -------------------------------------------------------------- what is written

def test_notes_come_from_the_text_blocks_only(ls, cfg, lecture, sdk):
    sdk()
    d, _ = lecture(bundle=bundle_with(1))
    ls.run_analysis(claude(cfg), d, use_slides=True, prompt=ls.DELTA_PROMPT)
    notes = (d / "notes.md").read_text()
    assert "the exam covers this" in notes
    assert "reasoning that is not notes" not in notes


def test_a_refusal_writes_nothing(ls, cfg, lecture, sdk):
    sdk(message=reply(stop="refusal", category="bio"))
    d, _ = lecture(bundle=bundle_with(1))
    with pytest.raises(RuntimeError, match="declined.*bio"):
        ls.run_analysis(claude(cfg), d, use_slides=True, prompt=ls.DELTA_PROMPT)
    assert not (d / "notes.md").exists()


def test_notes_cut_off_at_max_tokens_are_not_written(ls, cfg, lecture, sdk):
    """Truncated notes read as complete, which is worse than no notes."""
    sdk(message=reply(stop="max_tokens"))
    d, _ = lecture(bundle=bundle_with(1))
    with pytest.raises(RuntimeError, match="max_tokens"):
        ls.run_analysis(claude(cfg), d, use_slides=True, prompt=ls.DELTA_PROMPT)
    assert not (d / "notes.md").exists()


def test_a_fallback_is_reported(ls, cfg, lecture, sdk, capsys):
    switch = NS(type="fallback", from_=NS(model="claude-opus-5"),
                to=NS(model="claude-opus-4-8"))
    sdk(message=reply(model="claude-opus-4-8", extra=(switch,)))
    d, _ = lecture(bundle=bundle_with(1))
    ls.run_analysis(claude(cfg), d, use_slides=True, prompt=ls.DELTA_PROMPT)
    assert "claude-opus-5 declined; claude-opus-4-8 wrote" in capsys.readouterr().out
    assert (d / "notes.md").exists()


def test_the_cost_is_printed(ls, cfg, lecture, sdk, capsys):
    sdk()  # 40,000 in and 8,000 out at $5 / $25 per million
    d, _ = lecture(bundle=bundle_with(1))
    ls.run_analysis(claude(cfg), d, use_slides=True, prompt=ls.DELTA_PROMPT)
    assert "about $0.40" in capsys.readouterr().out


# ----------------------------------------------------- refused before spending

def test_a_local_model_tag_is_refused_before_any_request(ls, cfg, lecture, sdk):
    calls = sdk()
    d, _ = lecture(bundle=bundle_with(1))
    with pytest.raises(RuntimeError, match="not a Claude model id"):
        ls.run_analysis(claude(cfg, model="qwen3.8:27b-mlx"), d,
                        use_slides=True, prompt=ls.DELTA_PROMPT)
    assert calls == []


def test_missing_credentials_become_an_instruction(ls, cfg, lecture, sdk):
    sdk(raises=TypeError('"Could not resolve authentication method. Expected '
                         'one of api_key, auth_token..."'))
    d, _ = lecture(bundle=bundle_with(1))
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        ls.run_analysis(claude(cfg), d, use_slides=True, prompt=ls.DELTA_PROMPT)


def test_an_unrelated_type_error_is_not_swallowed(ls, cfg, lecture, sdk):
    sdk(raises=TypeError("unsupported operand type(s)"))
    d, _ = lecture(bundle=bundle_with(1))
    with pytest.raises(TypeError, match="unsupported operand"):
        ls.run_analysis(claude(cfg), d, use_slides=True, prompt=ls.DELTA_PROMPT)


# -------------------------------------------------------- the real SDK, on wire

def test_the_real_sdk_sends_the_intended_request(ls, cfg, tmp_path, monkeypatch):
    pytest.importorskip("anthropic")
    seen = {}

    class Capture(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            seen["beta"] = self.headers.get("anthropic-beta")
            seen["body"] = json.loads(self.rfile.read(int(self.headers["content-length"])))
            err = json.dumps({"type": "error", "error": {
                "type": "invalid_request_error", "message": "captured"}}).encode()
            self.send_response(400)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(err)))
            self.end_headers()
            self.wfile.write(err)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Capture)
    # serve_forever polls every 0.5s by default, and shutdown() waits out the
    # poll: half the runtime of the whole suite, for nothing.
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01},
                     daemon=True).start()
    monkeypatch.setenv("ANTHROPIC_BASE_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-a-real-key")
    img = tmp_path / "slide.jpg"
    img.write_bytes(b"\xff\xd8\xff\xe0")
    try:
        with pytest.raises(RuntimeError, match="rejected the request"):
            ls.claude_analysis(claude(cfg, effort="low"), tmp_path, "BODY", "PROMPT", [img])
    finally:
        server.shutdown()

    body = seen["body"]
    assert seen["beta"] == "server-side-fallback-2026-07-01"
    assert body["fallbacks"] == "default"
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"] == {"effort": "low"}
    assert body["stream"] is True
    assert [c["type"] for c in body["messages"][0]["content"]] == ["image", "text"]
