"use strict";

const assert = require("node:assert/strict");

require("../shared/connection-settings.js");
require("../shared/translation-client.js");

const settings = globalThis.STTConnectionSettings;

function expectResolved(rawUrl, apiMode, autoComplete, expectedUrl) {
  const resolved = settings.resolveApiUrl(rawUrl, apiMode, autoComplete);
  assert.equal(resolved.error, "", `unexpected error for ${rawUrl}: ${resolved.error}`);
  assert.equal(resolved.requestUrl, expectedUrl);
}

expectResolved(
  "",
  "chat_completions",
  true,
  "https://api.openai.com/v1/chat/completions"
);
expectResolved(
  "http://127.0.0.1:8000",
  "chat_completions",
  true,
  "http://127.0.0.1:8000/v1/chat/completions"
);
expectResolved(
  "http://127.0.0.1:8000/v1",
  "chat_completions",
  true,
  "http://127.0.0.1:8000/v1/chat/completions"
);
expectResolved(
  "https://provider.example/api/openai/",
  "responses",
  true,
  "https://provider.example/api/openai/v1/responses"
);
expectResolved(
  "https://provider.example/v1",
  "messages",
  true,
  "https://provider.example/v1/messages"
);
expectResolved(
  "https://provider.example/custom/v1/chat/completions?region=tw",
  "chat_completions",
  true,
  "https://provider.example/custom/v1/chat/completions?region=tw"
);
expectResolved(
  "https://provider.example/no-standard-suffix?mode=custom",
  "responses",
  false,
  "https://provider.example/no-standard-suffix?mode=custom"
);

const mismatch = settings.resolveApiUrl(
  "https://provider.example/v1/messages",
  "chat_completions",
  true
);
assert.equal(mismatch.requestUrl, "https://provider.example/v1/messages");
assert.match(mismatch.error, /endpoint is messages.*API type is chat_completions/i);
assert.match(
  settings.resolveApiUrl(
    "https://provider.example/v1/responses",
    "messages",
    false
  ).error,
  /endpoint is responses.*API type is messages/i
);
assert.equal(
  settings.resolveApiUrl(
    "https://provider.example/v1/responses",
    "responses",
    false
  ).autoComplete,
  false
);

assert.match(
  settings.resolveApiUrl("", "responses", false).error,
  /required/i
);
assert.match(
  settings.resolveApiUrl("provider.example/v1", "responses", true).error,
  /absolute/i
);

assert.deepEqual(
  settings.connectionFromModel({
    baseUrl: "https://provider.example/v1/responses",
  }),
  {
    apiUrl: "https://provider.example/v1/responses",
    apiMode: "responses",
    autoCompleteApiUrl: true,
  }
);

const sanitized = settings.sanitizeModel({
  id: "secret-model",
  name: "Secret model",
  engine: "openai",
  model: "served-model",
  apiKey: "must-not-remain",
  baseUrl: "https://provider.example/v1",
  apiMode: "responses",
  autoCompleteApiUrl: false,
});
assert.equal(sanitized.apiKey, undefined);
assert.equal(sanitized.baseUrl, undefined);
assert.equal(sanitized.hasApiKey, true);
assert.deepEqual(sanitized.connection, {
  apiUrl: "https://provider.example/v1",
  apiMode: "responses",
  autoCompleteApiUrl: false,
});
assert.doesNotMatch(JSON.stringify(sanitized), /must-not-remain/);

const meta = globalThis.STTTranslationClient.buildRequestMeta({
  target_language: "zh-TW",
  partial: true,
  openai: {
    model: "served-model",
    base_url: "http://localhost:8000/v1",
    api_mode: "responses",
    auto_complete_api_url: true,
  },
});
assert.equal(meta.apiUrl, "http://localhost:8000/v1/responses");
assert.equal(meta.apiMode, "responses");
assert.equal(meta.partial, true);

async function testRequestShapesAndRedaction() {
  let captured;
  globalThis.fetch = async (url, options) => {
    captured = { url, options, body: JSON.parse(options.body) };
    return {
      ok: true,
      status: 200,
      statusText: "OK",
      text: async () => JSON.stringify({ output_text: "譯文" }),
    };
  };
  await globalThis.STTTranslationClient.translate(
    {
      target_language: "zh-TW",
      openai: {
        model: "served-model",
        base_url: "https://provider.example/v1",
        api_mode: "responses",
        auto_complete_api_url: true,
      },
    },
    "Hello",
    "en"
  );
  assert.equal(captured.url, "https://provider.example/v1/responses");
  assert.equal(captured.body.model, "served-model");
  assert.equal(typeof captured.body.instructions, "string");
  assert.equal(typeof captured.body.input, "string");
  assert.equal(captured.body.messages, undefined);

  globalThis.fetch = async (url, options) => {
    captured = { url, options, body: JSON.parse(options.body) };
    return {
      ok: true,
      status: 200,
      statusText: "OK",
      text: async () => JSON.stringify({ content: [{ type: "text", text: "譯文" }] }),
    };
  };
  await globalThis.STTTranslationClient.translate(
    {
      openai: {
        model: "claude-model",
        base_url: "https://provider.example/v1",
        api_mode: "messages",
        auto_complete_api_url: true,
      },
    },
    "Hello",
    "en"
  );
  assert.equal(captured.url, "https://provider.example/v1/messages");
  assert.equal(captured.options.headers["anthropic-version"], "2023-06-01");
  assert.equal(Array.isArray(captured.body.messages), true);

  const secret = "do-not-leak-this-key";
  globalThis.fetch = async () => ({
    ok: false,
    status: 401,
    statusText: "Unauthorized",
    text: async () => JSON.stringify({ error: { message: `rejected ${secret}` } }),
  });
  await assert.rejects(
    globalThis.STTTranslationClient.translate(
      {
        openai: {
          api_key: secret,
          base_url: "https://provider.example/v1/chat/completions",
          api_mode: "chat_completions",
          auto_complete_api_url: true,
        },
      },
      "Hello",
      "en"
    ),
    (err) => {
      assert.doesNotMatch(err.message, new RegExp(secret));
      assert.match(err.message, /\[redacted\]/);
      return true;
    }
  );
}

testRequestShapesAndRedaction()
  .then(() => console.log("connection-settings tests passed"))
  .catch((err) => {
    console.error(err);
    process.exitCode = 1;
  });
