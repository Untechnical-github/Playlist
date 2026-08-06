const DISCORD_API = "https://discord.com/api/v10";

function hexToBytes(hex) {
  const bytes = new Uint8Array(hex.length / 2);
  for (let i = 0; i < bytes.length; i++) {
    bytes[i] = parseInt(hex.substr(i * 2, 2), 16);
  }
  return bytes;
}

async function verifyDiscordRequest(request, publicKeyHex) {
  const signature = request.headers.get("X-Signature-Ed25519");
  const timestamp = request.headers.get("X-Signature-Timestamp");
  const body = await request.clone().text();
  if (!signature || !timestamp) return { valid: false, body };

  const publicKey = await crypto.subtle.importKey(
    "raw",
    hexToBytes(publicKeyHex),
    { name: "Ed25519" },
    false,
    ["verify"]
  );

  const valid = await crypto.subtle.verify(
    "Ed25519",
    publicKey,
    hexToBytes(signature),
    new TextEncoder().encode(timestamp + body)
  );
  return { valid, body };
}

function json(obj) {
  return new Response(JSON.stringify(obj), {
    headers: { "content-type": "application/json" },
  });
}

async function getGoogleAccessToken(env) {
  const resp = await fetch("https://oauth2.googleapis.com/token", {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      client_id: env.GOOGLE_CLIENT_ID,
      client_secret: env.GOOGLE_CLIENT_SECRET,
      refresh_token: env.GOOGLE_REFRESH_TOKEN,
      grant_type: "refresh_token",
    }),
  });
  const data = await resp.json();
  if (!resp.ok) throw new Error("token refresh failed: " + JSON.stringify(data));
  return data.access_token;
}

async function handleAutocomplete(interaction, env) {
  const focused = (interaction.data.options || []).find((o) => o.focused);
  const query = (focused && focused.value ? focused.value : "").toLowerCase();

  let choices = [];
  try {
    const token = await getGoogleAccessToken(env);
    const resp = await fetch(
      "https://www.googleapis.com/youtube/v3/playlists?part=snippet&mine=true&maxResults=50",
      { headers: { Authorization: `Bearer ${token}` } }
    );
    const data = await resp.json();
    choices = (data.items || [])
      .filter((p) => p.snippet.title.toLowerCase().includes(query))
      .slice(0, 25)
      .map((p) => ({ name: p.snippet.title.slice(0, 100), value: p.id }));
  } catch (e) {
    choices = [];
  }

  return json({ type: 8, data: { choices } });
}

async function triggerGithubAction(env, payload) {
  const resp = await fetch(`https://api.github.com/repos/${env.GITHUB_REPO}/dispatches`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      Accept: "application/vnd.github+json",
      "content-type": "application/json",
      "User-Agent": "playlist-sort-worker",
    },
    body: JSON.stringify({ event_type: "discord-sort", client_payload: payload }),
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`GitHub dispatch failed: ${resp.status} ${text}`);
  }
}

async function handleCommand(interaction, env, ctx) {
  const playlistId = (interaction.data.options || []).find((o) => o.name === "playlist");
  if (!playlistId || !playlistId.value) {
    return json({ type: 4, data: { content: "プレイリストが指定されていません。" } });
  }

  const mode = interaction.data.name === "score" ? "score" : "preview";

  ctx.waitUntil(
    triggerGithubAction(env, {
      mode,
      playlist_id: playlistId.value,
      application_id: interaction.application_id,
      interaction_token: interaction.token,
    }).catch(() => {})
  );

  return json({ type: 5 });
}

async function handleComponent(interaction, env, ctx) {
  const customId = interaction.data.custom_id || "";
  const sep = customId.indexOf(":");
  const action = sep === -1 ? customId : customId.slice(0, sep);
  const playlistId = sep === -1 ? "" : customId.slice(sep + 1);

  if (action === "cancel") {
    return json({
      type: 7,
      data: { content: "キャンセルしました。", embeds: [], components: [] },
    });
  }

  if (action === "confirm") {
    ctx.waitUntil(
      triggerGithubAction(env, {
        mode: "apply",
        playlist_id: playlistId,
        application_id: interaction.application_id,
        interaction_token: interaction.token,
      }).catch(() => {})
    );
    return json({
      type: 7,
      data: { content: "反映中…", components: [] },
    });
  }

  return json({ type: 6 });
}

export default {
  async fetch(request, env, ctx) {
    if (request.method !== "POST") {
      return new Response("Discord interactions endpoint is running.", { status: 200 });
    }

    const { valid, body } = await verifyDiscordRequest(request, env.DISCORD_PUBLIC_KEY);
    if (!valid) {
      return new Response("invalid request signature", { status: 401 });
    }

    const interaction = JSON.parse(body);

    if (interaction.type === 1) {
      return json({ type: 1 });
    }
    if (interaction.type === 4) {
      return handleAutocomplete(interaction, env);
    }
    if (interaction.type === 2) {
      return handleCommand(interaction, env, ctx);
    }
    if (interaction.type === 3) {
      return handleComponent(interaction, env, ctx);
    }

    return json({ type: 4, data: { content: "未対応のインタラクションです。" } });
  },
};
