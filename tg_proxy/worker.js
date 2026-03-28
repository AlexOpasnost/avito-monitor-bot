// Cloudflare Worker — Telegram API Proxy (streaming)
export default {
  async fetch(request) {
    const url = new URL(request.url);
    const tgUrl = "https://api.telegram.org" + url.pathname + url.search;

    // Clone headers, remove problematic ones
    const headers = new Headers(request.headers);
    headers.delete("host");
    headers.delete("cf-connecting-ip");
    headers.delete("cf-ray");
    headers.delete("cf-visitor");

    const init = {
      method: request.method,
      headers: headers,
    };

    if (request.method !== "GET" && request.method !== "HEAD") {
      init.body = request.body;
      init.duplex = "half";
    }

    const response = await fetch(tgUrl, init);
    return new Response(response.body, {
      status: response.status,
      headers: response.headers,
    });
  },
};
