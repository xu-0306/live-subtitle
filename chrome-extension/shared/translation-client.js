(function (global) {
  "use strict";

  const DEFAULT_OPENAI_ENDPOINT = "https://api.openai.com/v1/chat/completions";
  const DEFAULT_ANTHROPIC_VERSION = "2023-06-01";
  const SYSTEM_TRANSLATION_PROMPT =
    "You are a translation engine. Follow the user's instructions exactly.";

  const LANGUAGE_LABELS = {
    auto: "auto-detected language",
    en: "English (en)",
    "en-us": "English (en-US)",
    "en-gb": "English (en-GB)",
    ja: "Japanese (ja)",
    jp: "Japanese (ja)",
    jpn: "Japanese (ja)",
    zh: "Chinese (zh)",
    "zh-cn": "Simplified Chinese (zh-CN)",
    "zh-hans": "Simplified Chinese (zh-Hans)",
    "zh-tw": "Traditional Chinese (zh-TW)",
    "zh-hant": "Traditional Chinese (zh-Hant)",
    "zh-hk": "Traditional Chinese (zh-HK)",
    ko: "Korean (ko)",
    fr: "French (fr)",
    de: "German (de)",
    es: "Spanish (es)",
    pt: "Portuguese (pt)",
    it: "Italian (it)",
    ru: "Russian (ru)",
    id: "Indonesian (id)",
    vi: "Vietnamese (vi)",
    th: "Thai (th)",
  };

  function describeLanguage(lang, fallback) {
    if (!lang) return fallback;
    const normalized = String(lang).replaceAll("_", "-").trim().toLowerCase();
    return LANGUAGE_LABELS[normalized] || String(lang).trim();
  }

  function buildTranslationPrompt(text, sourceLang, targetLang) {
    const sourceDesc = describeLanguage(sourceLang, "source language");
    const targetDesc = describeLanguage(targetLang, "target language");
    return (
      `Translate the following ${sourceDesc} text into ${targetDesc}. ` +
      `Output only the translation in ${targetDesc}. ` +
      "Do not include the source text, explanations, or extra commentary. " +
      "Do not mix other languages. " +
      "Preserve meaning, punctuation, numbers, and proper nouns. " +
      `If the input is already in ${targetDesc}, return it unchanged.\n\n` +
      text
    );
  }

  function normalizeApiUrl(rawUrl, apiMode, autoComplete) {
    if (global.STTConnectionSettings) {
      const resolved = global.STTConnectionSettings.resolveApiUrl(
        rawUrl,
        apiMode,
        autoComplete
      );
      return {
        apiUrl: resolved.requestUrl,
        apiMode: resolved.apiMode,
        apiUrlError: resolved.error,
      };
    }
    const stripped = String(rawUrl || "").trim() || DEFAULT_OPENAI_ENDPOINT;
    return {
      apiUrl: stripped,
      apiMode: String(apiMode || "chat_completions"),
      apiUrlError: "",
    };
  }

  function coerceText(value) {
    if (typeof value === "string") {
      return value.trim();
    }
    if (Array.isArray(value)) {
      return value.map(coerceText).filter(Boolean).join("\n").trim();
    }
    if (value && typeof value === "object") {
      for (const key of ["output_text", "text", "content", "value"]) {
        if (key in value) {
          const text = coerceText(value[key]);
          if (text) return text;
        }
      }
    }
    return "";
  }

  function extractResponsesText(payload) {
    const direct = coerceText(payload?.output_text);
    if (direct) return direct;
    const output = Array.isArray(payload?.output) ? payload.output : [];
    for (const item of output) {
      const text = coerceText(item?.content);
      if (text) return text;
    }
    return "";
  }

  function extractMessagesText(payload) {
    return coerceText(payload?.content);
  }

  function extractChatCompletionText(payload) {
    const choices = Array.isArray(payload?.choices) ? payload.choices : [];
    for (const choice of choices) {
      const text = coerceText(choice?.message?.content);
      if (text) return text;
    }
    return "";
  }

  function extractProviderError(payload) {
    const error = payload?.error;
    if (error && typeof error === "object") {
      const message = coerceText(error.message);
      if (message) return message;
      return JSON.stringify(error);
    }
    const text = coerceText(error);
    if (text) return text;
    return JSON.stringify(payload);
  }

  function redactSecret(value, secret) {
    const text = String(value || "");
    return secret ? text.replaceAll(secret, "[redacted]") : text;
  }

  function buildRequestMeta(translationCfg) {
    const openaiCfg =
      translationCfg && translationCfg.openai && typeof translationCfg.openai === "object"
        ? translationCfg.openai
        : {};
    const model = String(openaiCfg.model || "gpt-4o-mini");
    const rawUrl = openaiCfg.base_url || openaiCfg.api_url || "";
    const requestedMode = openaiCfg.api_mode || openaiCfg.apiMode;
    const autoComplete =
      (openaiCfg.auto_complete_api_url ?? openaiCfg.autoCompleteApiUrl) !== false;
    const { apiUrl, apiMode, apiUrlError } = normalizeApiUrl(
      rawUrl,
      requestedMode,
      autoComplete
    );
    return {
      model,
      apiUrl,
      apiMode,
      apiUrlError,
      autoCompleteApiUrl: autoComplete,
      apiKey: String(openaiCfg.api_key || ""),
      targetLanguage: String(translationCfg?.target_language || "zh-TW"),
      partial: Boolean(translationCfg?.partial),
    };
  }

  function describeConfig(translationCfg) {
    const meta = buildRequestMeta(translationCfg);
    return `engine=openai model=${meta.model} mode=${meta.apiMode} url=${meta.apiUrl}`;
  }

  async function translate(translationCfg, text, sourceLang) {
    const meta = buildRequestMeta(translationCfg);
    if (meta.apiUrlError) {
      throw new Error(meta.apiUrlError);
    }
    const prompt = buildTranslationPrompt(text, sourceLang, meta.targetLanguage);
    const headers = {
      "Content-Type": "application/json",
      Accept: "application/json",
    };
    if (meta.apiKey) {
      headers.Authorization = `Bearer ${meta.apiKey}`;
      headers["x-api-key"] = meta.apiKey;
    }

    let body;
    if (meta.apiMode === "responses") {
      body = {
        model: meta.model,
        instructions: SYSTEM_TRANSLATION_PROMPT,
        input: prompt,
        temperature: 0.2,
      };
    } else if (meta.apiMode === "messages") {
      headers["anthropic-version"] = DEFAULT_ANTHROPIC_VERSION;
      headers["anthropic-dangerous-direct-browser-access"] = "true";
      body = {
        model: meta.model,
        system: SYSTEM_TRANSLATION_PROMPT,
        messages: [{ role: "user", content: prompt }],
        max_tokens: 1024,
        temperature: 0.2,
      };
    } else {
      body = {
        model: meta.model,
        messages: [
          { role: "system", content: SYSTEM_TRANSLATION_PROMPT },
          { role: "user", content: prompt },
        ],
        temperature: 0.2,
      };
    }

    const response = await fetch(meta.apiUrl, {
      method: "POST",
      headers,
      body: JSON.stringify(body),
    });

    const raw = await response.text();
    let payload;
    try {
      payload = raw ? JSON.parse(raw) : {};
    } catch {
      payload = null;
    }
    if (!response.ok) {
      const reason = payload ? extractProviderError(payload) : raw;
      throw new Error(
        redactSecret(`${response.status} ${response.statusText}: ${reason}`, meta.apiKey)
      );
    }

    let translated = "";
    if (meta.apiMode === "responses") {
      translated = extractResponsesText(payload);
    } else if (meta.apiMode === "messages") {
      translated = extractMessagesText(payload);
    } else {
      translated = extractChatCompletionText(payload);
    }
    return translated.trim();
  }

  global.STTTranslationClient = {
    buildRequestMeta,
    describeConfig,
    translate,
  };
})(globalThis);
