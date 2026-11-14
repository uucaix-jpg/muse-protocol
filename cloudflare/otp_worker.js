/**
 * Cloudflare Email Worker + HTTP API for automatic OTP extraction.
 *
 * Bindings required:
 *   - OTP_KV (KV Namespace)
 *   - API_KEY (Secret / Plain Text env var for Bearer auth)
 *   - FORWARD_TO (Optional env var, e.g. "your@gmail.com", forwards every email)
 */

function decodeHeaderWord(str) {
  if (!str) return "";
  return str.replace(/=\?([^?]+)\?([BQbq])\?([^?]+)\?=/g, (_, charset, enc, data) => {
    try {
      let bytes;
      if (enc.toUpperCase() === "B") {
        const bin = atob(data);
        bytes = Uint8Array.from(bin, (c) => c.charCodeAt(0));
      } else {
        const qp = data.replace(/_/g, " ");
        const arr = [];
        for (let i = 0; i < qp.length; i++) {
          if (qp[i] === "=" && /^[0-9A-Fa-f]{2}$/.test(qp.slice(i + 1, i + 3))) {
            arr.push(parseInt(qp.slice(i + 1, i + 3), 16));
            i += 2;
          } else {
            arr.push(qp.charCodeAt(i));
          }
        }
        bytes = new Uint8Array(arr);
      }
      return new TextDecoder(charset || "utf-8").decode(bytes);
    } catch {
      return data;
    }
  });
}

function decodeQuotedPrintable(str, charset = "utf-8") {
  const unfolded = str.replace(/=\r?\n/g, "");
  const bytes = [];
  for (let i = 0; i < unfolded.length; i++) {
    if (unfolded[i] === "=" && /^[0-9A-Fa-f]{2}$/.test(unfolded.slice(i + 1, i + 3))) {
      bytes.push(parseInt(unfolded.slice(i + 1, i + 3), 16));
      i += 2;
    } else {
      bytes.push(unfolded.charCodeAt(i) & 0xff);
    }
  }
  try {
    return new TextDecoder(charset).decode(new Uint8Array(bytes));
  } catch {
    return new TextDecoder("utf-8").decode(new Uint8Array(bytes));
  }
}

function decodeBase64(str, charset = "utf-8") {
  try {
    const clean = str.replace(/\s+/g, "");
    const bin = atob(clean);
    const bytes = Uint8Array.from(bin, (c) => c.charCodeAt(0));
    return new TextDecoder(charset).decode(bytes);
  } catch {
    return str;
  }
}

function stripHtml(html) {
  return html
    .replace(/<style[\s\S]*?<\/style>/gi, " ")
    .replace(/<script[\s\S]*?<\/script>/gi, " ")
    .replace(/<[^>]+>/g, " ")
    .replace(/&nbsp;/gi, " ")
    .replace(/&amp;/gi, "&")
    .replace(/&lt;/gi, "<")
    .replace(/&gt;/gi, ">")
    .replace(/&#(\d+);/g, (_, n) => String.fromCharCode(Number(n)))
    .replace(/\s+/g, " ")
    .trim();
}

function parsePart(rawPart) {
  const sepIdx = rawPart.search(/\r?\n\r?\n/);
  if (sepIdx === -1) return { headers: "", body: rawPart };
  const sepLen = rawPart.slice(sepIdx).startsWith("\r\n\r\n") ? 4 : 2;
  return {
    headers: rawPart.slice(0, sepIdx),
    body: rawPart.slice(sepIdx + sepLen),
  };
}

function extractReadableText(rawEmail) {
  const { headers, body } = parsePart(rawEmail);
  const unfoldedHeaders = headers.replace(/\r?\n[ \t]+/g, " ");
  const boundaryMatch = unfoldedHeaders.match(/boundary="?([^";\r\n]+)"?/i);

  const chunks = [];
  if (boundaryMatch) {
    const parts = body.split("--" + boundaryMatch[1]);
    for (const part of parts) {
      const trimmed = part.trim();
      if (!trimmed || trimmed === "--") continue;
      if (/boundary=/i.test(trimmed.slice(0, 500))) {
        chunks.push(extractReadableText(trimmed));
        continue;
      }
      const p = parsePart(trimmed);
      const ph = p.headers.replace(/\r?\n[ \t]+/g, " ");
      const ct = (ph.match(/content-type:\s*([^;\r\n]+)/i)?.[1] || "text/plain").toLowerCase();
      if (!ct.startsWith("text/")) continue;
      const charset = ph.match(/charset="?([^";\r\n]+)"?/i)?.[1] || "utf-8";
      const enc = (ph.match(/content-transfer-encoding:\s*([^\r\n]+)/i)?.[1] || "").trim().toLowerCase();
      let text = p.body;
      if (enc === "base64") text = decodeBase64(text, charset);
      else if (enc === "quoted-printable") text = decodeQuotedPrintable(text, charset);
      if (ct === "text/html") text = stripHtml(text);
      chunks.push(text.trim());
    }
  } else {
    const ct = (unfoldedHeaders.match(/content-type:\s*([^;\r\n]+)/i)?.[1] || "text/plain").toLowerCase();
    const charset = unfoldedHeaders.match(/charset="?([^";\r\n]+)"?/i)?.[1] || "utf-8";
    const enc = (unfoldedHeaders.match(/content-transfer-encoding:\s*([^\r\n]+)/i)?.[1] || "").trim().toLowerCase();
    let text = body;
    if (enc === "base64") text = decodeBase64(text, charset);
    else if (enc === "quoted-printable") text = decodeQuotedPrintable(text, charset);
    if (ct === "text/html") text = stripHtml(text);
    chunks.push(text.trim());
  }
  return chunks.filter(Boolean).join("\n");
}

function extractOtp(subject, text) {
  const combined = `${subject}\n${text}`;
  // Remove URLs and hex color codes before matching 6-digit OTPs
  const cleaned = combined
    .replace(/https?:\/\/\S+/gi, " ")
    .replace(/#[0-9a-fA-F]{6}\b/g, " ");

  const contextual = cleaned.match(
    /(?:code|otp|verification|verify|pin|認証コード|確認コード|验证码)[^\d]{0,30}(?<!\d)(\d{6})(?!\d)/i
  );
  if (contextual) return contextual[1];

  const standalone = cleaned.match(/(?<!\d)(\d{6})(?!\d)/);
  return standalone ? standalone[1] : null;
}

function unauthorized() {
  return new Response(JSON.stringify({ ok: false, error: "unauthorized" }), {
    status: 401,
    headers: { "content-type": "application/json" },
  });
}

function jsonResponse(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json" },
  });
}

export default {
  async email(message, env, ctx) {
    const raw = await new Response(message.raw).text();
    const subject = decodeHeaderWord(message.headers.get("subject") || "");
    const text = extractReadableText(raw);
    const otp = extractOtp(subject, text);
    const to = (message.to || "").toLowerCase().trim();
    const record = {
      to,
      from: message.from || "",
      subject,
      otp,
      preview: text.slice(0, 1500),
      received_at: Date.now(),
    };

    if (env.OTP_KV && to) {
      await env.OTP_KV.put(`inbox:${to}`, JSON.stringify(record), { expirationTtl: 3600 });
      await env.OTP_KV.put("latest", JSON.stringify(record), { expirationTtl: 3600 });
    }

    if (env.FORWARD_TO) {
      await message.forward(env.FORWARD_TO);
    }
  },

  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.pathname === "/health") {
      return jsonResponse({ ok: true });
    }

    const authHeader = request.headers.get("authorization") || "";
    const bearer = authHeader.startsWith("Bearer ") ? authHeader.slice(7).trim() : "";
    const queryToken = url.searchParams.get("token") || "";
    const provided = bearer || queryToken;
    if (!env.API_KEY || provided !== env.API_KEY) {
      return unauthorized();
    }

    if (url.pathname === "/otp") {
      const to = (url.searchParams.get("to") || "").toLowerCase().trim();
      if (!to) {
        return jsonResponse({ ok: false, error: "missing 'to' query param" }, 400);
      }
      if (request.method === "DELETE") {
        await env.OTP_KV.delete(`inbox:${to}`);
        return jsonResponse({ ok: true, deleted: to });
      }
      const raw = await env.OTP_KV.get(`inbox:${to}`);
      if (!raw) {
        return jsonResponse({ ok: true, found: false, to });
      }
      const record = JSON.parse(raw);
      const after = Number(url.searchParams.get("after") || "0");
      if (after && record.received_at < after) {
        return jsonResponse({ ok: true, found: false, to, stale: true });
      }
      return jsonResponse({ ok: true, found: true, ...record });
    }

    if (url.pathname === "/latest") {
      const raw = await env.OTP_KV.get("latest");
      if (!raw) return jsonResponse({ ok: true, found: false });
      return jsonResponse({ ok: true, found: true, ...JSON.parse(raw) });
    }

    return jsonResponse({ ok: false, error: "not_found" }, 404);
  },
};
