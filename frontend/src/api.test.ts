import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { api, ApiError, request } from "./api";

const originalFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = originalFetch; });

test("writes use same-origin cookies and JSON without client-set Origin or bearer token", async () => {
  let received: RequestInit | undefined;
  globalThis.fetch = async (_url, options) => {
    received = options;
    return new Response(JSON.stringify({ authenticated: true }), { status: 200 });
  };
  const result = await api.login("test-only", new AbortController().signal);
  assert.equal(result.authenticated, true);
  assert.equal(received?.credentials, "same-origin");
  assert.equal(received?.cache, "no-store");
  assert.equal(received?.redirect, "error");
  assert.deepEqual(JSON.parse(String(received?.body)), { password: "test-only" });
  assert.equal(new Headers(received?.headers).has("Origin"), false);
  assert.equal(new Headers(received?.headers).has("Authorization"), false);
});

test("typed API errors retain status and expose conflicts, expiration and service errors", async () => {
  for (const status of [401, 403, 409, 429, 503]) {
    globalThis.fetch = async () => new Response(JSON.stringify({ error: "test_error", detail: "Detalhe verificável" }), { status });
    await assert.rejects(request("/api/snapshot"), (error: unknown) => {
      assert.ok(error instanceof ApiError);
      assert.equal(error.status, status);
      assert.equal(error.code, "test_error");
      assert.match(error.message, /Detalhe verificável/);
      return true;
    });
  }
});

test("malformed JSON and network failures never produce a fallback snapshot", async () => {
  globalThis.fetch = async () => new Response("<html>Not an API</html>", { status: 200 });
  await assert.rejects(api.snapshot(new AbortController().signal), (error: unknown) =>
    error instanceof ApiError && error.code === "invalid_response");
  globalThis.fetch = async () => { throw new TypeError("network down"); };
  await assert.rejects(api.snapshot(new AbortController().signal), (error: unknown) =>
    error instanceof ApiError && error.status === 0);
});

test("caller cancellation reaches fetch and does not become a false connection error", async () => {
  const controller = new AbortController();
  const reason = new Error("Caller cancelled stale request");
  globalThis.fetch = async (_url, options) => new Promise((_resolve, reject) => {
    options?.signal?.addEventListener("abort", () => reject(options.signal?.reason), { once: true });
  });
  const pending = api.snapshot(controller.signal);
  controller.abort(reason);
  await assert.rejects(pending, reason);
});

test("contract control and explicit intervention approval send exact identifiers", async () => {
  const requests: Array<{ url: string; body: unknown }> = [];
  globalThis.fetch = async (url, options) => {
    requests.push({ url: String(url), body: options?.body ? JSON.parse(String(options.body)) : null });
    return new Response("{}", { status: 200 });
  };
  const signal = new AbortController().signal;
  await api.control("pause", 5, signal);
  await api.intervene("run-test", "recommendation-test", "reposition-denim", signal);
  await api.run("experiment", "Pergunta de teste", signal);
  assert.deepEqual(requests, [
    { url: "/api/scenario/control", body: { action: "pause", speed: 5 } },
    { url: "/api/interventions", body: { runId: "run-test", recommendationId: "recommendation-test", actionId: "reposition-denim" } },
    { url: "/api/agents/run", body: { agent: "experiment", question: "Pergunta de teste" } },
  ]);
});

test("logout accepts a body-free 204 response", async () => {
  globalThis.fetch = async () => new Response(null, { status: 204 });
  assert.equal(await api.logout(new AbortController().signal), undefined);
});
