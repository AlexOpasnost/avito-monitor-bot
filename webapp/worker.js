export default {
  async fetch(request) {
    const html = `<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>Avito Monitor</title>
<script src="https://telegram.org/js/telegram-web-app.js"><\/script>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family:system-ui,sans-serif; background:#0f0f1a; color:#e8e8f0; min-height:100vh; padding:16px; }
.wrap { max-width:480px; margin:0 auto; }
h1 { text-align:center; font-size:20px; margin:16px 0 8px; }
.ver { text-align:center; font-size:11px; color:#555; margin-bottom:16px; }
textarea { width:100%; min-height:120px; background:#16213e; border:1px solid #333; border-radius:10px; padding:12px; color:#e8e8f0; font-size:14px; resize:vertical; outline:none; }
textarea:focus { border-color:#4CAF50; }
.btn { width:100%; padding:16px; margin-top:12px; background:#4CAF50; color:#fff; font-size:16px; font-weight:600; border:none; border-radius:12px; cursor:pointer; }
.btn:active { background:#45a049; }
.msg { margin-top:12px; padding:12px; border-radius:10px; font-size:14px; display:none; }
.msg.ok { display:block; background:rgba(76,175,80,0.15); color:#4CAF50; }
.msg.err { display:block; background:rgba(239,68,68,0.15); color:#ef4444; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Avito Monitor</h1>
  <div class="ver">v3 — 2026-04-12</div>
  <textarea id="inp" placeholder="Вставьте ссылку с Авито сюда..."></textarea>
  <button class="btn" id="btn" onclick="go()">Добавить отслеживание</button>
  <div class="msg" id="msg"></div>
</div>
<script>
var tg = window.Telegram && window.Telegram.WebApp;
if (tg) { tg.ready(); tg.expand(); }

function go() {
  var raw = document.getElementById('inp').value || '';
  var msg = document.getElementById('msg');

  if (!raw.trim()) {
    msg.className = 'msg err';
    msg.textContent = 'Поле пустое — вставьте ссылку';
    return;
  }

  // Clean URL
  var url = raw.replace(/~/g, '-').replace(/\\s+/g, '');
  var m = url.match(/https?:\\/\\/[^\\s]+avito\\.ru\\/[^\\s]+/i);
  if (m) url = m[0];

  // Try sendData
  var sent = false;
  try {
    if (tg && tg.sendData) {
      tg.sendData(JSON.stringify({ url: url }));
      sent = true;
    }
  } catch(e) {
    msg.className = 'msg err';
    msg.textContent = 'sendData error: ' + e.message;
  }

  if (sent) {
    msg.className = 'msg ok';
    msg.textContent = 'Отправлено! Закрываю...';
    setTimeout(function() { if (tg) tg.close(); }, 1500);
  } else if (!msg.textContent) {
    // Fallback — copy to clipboard
    try {
      navigator.clipboard.writeText(url).then(function() {
        msg.className = 'msg ok';
        msg.textContent = 'Ссылка скопирована! Вставьте в чат бота.';
      }).catch(function() {
        msg.className = 'msg err';
        msg.textContent = 'Отправьте боту: ' + url.substring(0, 80) + '...';
      });
    } catch(e) {
      msg.className = 'msg err';
      msg.textContent = 'Отправьте боту: ' + url.substring(0, 80) + '...';
    }
    setTimeout(function() { if (tg) tg.close(); }, 4000);
  }
}
<\/script>
</body>
</html>`;
    return new Response(html, {
      headers: {
        "Content-Type": "text/html;charset=UTF-8",
        "Cache-Control": "no-cache, no-store, must-revalidate",
      },
    });
  },
};
