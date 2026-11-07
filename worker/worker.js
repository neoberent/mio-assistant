// Mio's clip links. Discord reads /c/<id> and plays the video from a short-lived link to the private B2 bucket,
// so the video itself never passes through Cloudflare (its CDN terms restrict that on free plans).
const enc = new TextEncoder();
const hex = (b) => [...new Uint8Array(b)].map((x) => x.toString(16).padStart(2, "0")).join("");
const sha256 = async (s) => hex(await crypto.subtle.digest("SHA-256", enc.encode(s)));
const hmac = async (key, msg) =>
  crypto.subtle.sign("HMAC", await crypto.subtle.importKey("raw", key, { name: "HMAC", hash: "SHA-256" }, false, ["sign"]), enc.encode(msg));

async function signedUrl(env, method, key, ttl = 3600) {
  const host = `${env.B2_BUCKET}.${env.B2_ENDPOINT}`;
  const region = env.B2_ENDPOINT.split(".")[1];
  const amz = new Date().toISOString().replace(/[-:]|\.\d+/g, "");
  const day = amz.slice(0, 8);
  const scope = `${day}/${region}/s3/aws4_request`;
  const params = new URLSearchParams({
    "X-Amz-Algorithm": "AWS4-HMAC-SHA256", "X-Amz-Credential": `${env.B2_READ_KEY_ID}/${scope}`, "X-Amz-Date": amz,
    "X-Amz-Expires": String(ttl), "X-Amz-SignedHeaders": "host",
  });
  params.sort();
  const query = params.toString().replace(/\+/g, "%20");
  const request = [method, `/${key}`, query, `host:${host}\n`, "host", "UNSIGNED-PAYLOAD"].join("\n");
  const toSign = ["AWS4-HMAC-SHA256", amz, scope, await sha256(request)].join("\n");
  let k = enc.encode(`AWS4${env.B2_READ_KEY}`);
  for (const part of [day, region, "s3", "aws4_request"]) k = await hmac(k, part);
  return `https://${host}/${key}?${query}&X-Amz-Signature=${hex(await hmac(k, toSign))}`;
}

const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const length = (s) => `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, "0")}`;

function page(origin, id, meta) {
  const title = esc(meta.title || "Clip");
  const game = esc(meta.game || "");
  const video = `${origin}/v/${id}.mp4`;
  const image = `${origin}/t/${id}.jpg`;
  const about = [meta.game && meta.game !== meta.title ? game : "", meta.seconds ? length(meta.seconds) : ""].filter(Boolean).join(" · ");
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>${title}</title><meta name="robots" content="noindex">
<meta property="og:site_name" content="Mio"><meta property="og:title" content="${title}">
${about ? `<meta property="og:description" content="${about}">` : ""}
<meta property="og:type" content="video.other"><meta property="og:url" content="${origin}/c/${id}"><meta property="og:image" content="${image}">
<meta property="og:video" content="${video}"><meta property="og:video:secure_url" content="${video}"><meta property="og:video:type" content="video/mp4">
<meta property="og:video:width" content="${Number(meta.width) || 1920}"><meta property="og:video:height" content="${Number(meta.height) || 1080}">
<meta name="twitter:card" content="player"><meta name="theme-color" content="#f5c2e7">
<style>body{margin:0;min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:12px;padding:16px;box-sizing:border-box;background:#1e1e2e;color:#cdd6f4;font:16px system-ui,sans-serif}
video{width:min(100%,1280px);border-radius:12px;background:#000}h1{margin:0;font-size:20px;color:#f5c2e7}p{margin:0;color:#a6adc8}</style></head>
<body><video controls playsinline preload="metadata" src="${video}" poster="${image}"></video><h1>${title}</h1>${about ? `<p>${about}</p>` : ""}</body></html>`;
}

const notFound = () => new Response("clip not found", { status: 404, headers: { "cache-control": "no-store" } });

export default {
  async fetch(request, env) {
    if (!["GET", "HEAD"].includes(request.method)) return new Response("no", { status: 405 });
    const url = new URL(request.url);
    const m = url.pathname.match(/^\/(c|v|t)\/([A-Za-z0-9_-]{8,40})(\.mp4|\.jpg)?$/);
    if (!m) return notFound();
    const [, kind, id] = m;
    if (kind === "v") {
      const r = Response.redirect(await signedUrl(env, request.method, `clips/${id}.mp4`), 302);
      return new Response(null, { status: 302, headers: { location: r.headers.get("location"), "cache-control": "no-store" } });
    }
    const key = kind === "t" ? `clips/${id}.jpg` : `clips/${id}.json`;
    const stored = await fetch(await signedUrl(env, "GET", key));  // always GET: HEAD through a Worker fails its signature
    if (!stored.ok) {
      await stored.body?.cancel();
      return stored.status === 404 ? notFound() : new Response("storage error", { status: 502 });
    }
    if (kind === "t") {
      return new Response(request.method === "HEAD" ? null : stored.body, { headers: { "content-type": "image/jpeg", "cache-control": "public, max-age=86400" } });
    }
    const html = page(url.origin, id, await stored.json());
    return new Response(request.method === "HEAD" ? null : html, { headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" } });
  },
};
