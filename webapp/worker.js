// Cloudflare Worker — serves the Avito Monitor Telegram WebApp
// Deploy: wrangler deploy --name avito-monitor-webapp

const HTML = `PLACEHOLDER`;

export default {
  async fetch(request) {
    return new Response(HTML, {
      headers: {
        "Content-Type": "text/html;charset=UTF-8",
        "Access-Control-Allow-Origin": "*",
      },
    });
  },
};
