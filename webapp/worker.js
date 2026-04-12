export default {
  async fetch(request) {
    const html = `<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>Avito Monitor</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  :root {
    --bg-primary: #0f0f1a;
    --bg-card: #1a1a2e;
    --bg-input: #16213e;
    --text-primary: #e8e8f0;
    --text-secondary: #8888a0;
    --text-muted: #55556a;
    --accent: #4CAF50;
    --accent-hover: #45a049;
    --accent-glow: rgba(76, 175, 80, 0.25);
    --error: #ef4444;
    --error-bg: rgba(239, 68, 68, 0.1);
    --warning: #f59e0b;
    --warning-bg: rgba(245, 158, 11, 0.1);
    --success-bg: rgba(76, 175, 80, 0.1);
    --border: rgba(255, 255, 255, 0.06);
    --border-focus: rgba(76, 175, 80, 0.5);
    --radius: 14px;
    --radius-sm: 10px;
  }

  * {
    margin: 0;
    padding: 0;
    box-sizing: border-box;
    -webkit-tap-highlight-color: transparent;
  }

  body {
    font-family: system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: var(--bg-primary);
    color: var(--text-primary);
    min-height: 100vh;
    padding: 0;
    overflow-x: hidden;
    line-height: 1.5;
  }

  .container {
    max-width: 480px;
    margin: 0 auto;
    padding: 20px 16px 40px;
  }

  /* Header */
  .header {
    text-align: center;
    padding: 24px 0 20px;
  }

  .header-logo {
    width: 56px;
    height: 56px;
    background: linear-gradient(135deg, var(--accent), #2e7d32);
    border-radius: 16px;
    display: flex;
    align-items: center;
    justify-content: center;
    margin: 0 auto 14px;
    font-size: 28px;
    box-shadow: 0 8px 24px var(--accent-glow);
  }

  .header h1 {
    font-size: 22px;
    font-weight: 700;
    letter-spacing: -0.3px;
    color: var(--text-primary);
  }

  .header p {
    font-size: 14px;
    color: var(--text-secondary);
    margin-top: 4px;
  }

  /* Steps */
  .steps {
    background: var(--bg-card);
    border-radius: var(--radius);
    padding: 18px 20px;
    margin-bottom: 20px;
    border: 1px solid var(--border);
  }

  .step {
    display: flex;
    align-items: flex-start;
    gap: 14px;
    padding: 10px 0;
  }

  .step + .step {
    border-top: 1px solid var(--border);
  }

  .step-num {
    width: 28px;
    height: 28px;
    min-width: 28px;
    background: linear-gradient(135deg, var(--accent), #2e7d32);
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 13px;
    font-weight: 700;
    color: #fff;
    margin-top: 1px;
  }

  .step-text {
    font-size: 14px;
    color: var(--text-secondary);
    line-height: 1.55;
  }

  .step-text strong {
    color: var(--text-primary);
    font-weight: 600;
  }

  /* Input card */
  .input-card {
    background: var(--bg-card);
    border-radius: var(--radius);
    padding: 20px;
    margin-bottom: 16px;
    border: 1px solid var(--border);
    transition: border-color 0.3s ease;
  }

  .input-card.focused {
    border-color: var(--border-focus);
  }

  .input-card.has-error {
    border-color: var(--error);
  }

  .input-card.has-success {
    border-color: var(--accent);
  }

  .input-label {
    font-size: 13px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: var(--text-muted);
    margin-bottom: 10px;
    display: block;
  }

  .url-input {
    width: 100%;
    min-height: 100px;
    background: var(--bg-input);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 14px 16px;
    font-family: 'SF Mono', 'Fira Code', 'Consolas', monospace;
    font-size: 13px;
    color: var(--text-primary);
    resize: vertical;
    outline: none;
    transition: border-color 0.3s ease, box-shadow 0.3s ease;
    line-height: 1.6;
  }

  .url-input::placeholder {
    color: var(--text-muted);
    font-family: system-ui, -apple-system, sans-serif;
    font-size: 14px;
  }

  .url-input:focus {
    border-color: var(--border-focus);
    box-shadow: 0 0 0 3px var(--accent-glow);
  }

  /* Validation feedback */
  .feedback {
    margin-top: 12px;
    display: none;
    animation: slideIn 0.25s ease;
  }

  .feedback.visible {
    display: block;
  }

  @keyframes slideIn {
    from { opacity: 0; transform: translateY(-6px); }
    to   { opacity: 1; transform: translateY(0); }
  }

  .feedback-item {
    display: flex;
    align-items: flex-start;
    gap: 8px;
    padding: 10px 14px;
    border-radius: var(--radius-sm);
    margin-top: 8px;
    font-size: 13px;
    line-height: 1.5;
  }

  .feedback-item:first-child {
    margin-top: 0;
  }

  .feedback-item.success {
    background: var(--success-bg);
    color: var(--accent);
  }

  .feedback-item.error {
    background: var(--error-bg);
    color: var(--error);
  }

  .feedback-item.warning {
    background: var(--warning-bg);
    color: var(--warning);
  }

  .feedback-icon {
    font-size: 15px;
    min-width: 18px;
    text-align: center;
    margin-top: 1px;
  }

  /* Parsed info */
  .parsed-info {
    margin-top: 12px;
    display: none;
    animation: slideIn 0.25s ease;
  }

  .parsed-info.visible {
    display: block;
  }

  .parsed-tag {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    background: rgba(76, 175, 80, 0.08);
    border: 1px solid rgba(76, 175, 80, 0.15);
    color: var(--text-secondary);
    font-size: 12px;
    padding: 5px 12px;
    border-radius: 20px;
    margin: 4px 4px 0 0;
  }

  .parsed-tag .tag-icon {
    font-size: 13px;
  }

  /* Submit button */
  .submit-btn {
    width: 100%;
    padding: 16px;
    background: linear-gradient(135deg, var(--accent), #2e7d32);
    color: #fff;
    font-size: 16px;
    font-weight: 600;
    border: none;
    border-radius: var(--radius);
    cursor: pointer;
    transition: all 0.3s ease;
    position: relative;
    overflow: hidden;
    letter-spacing: -0.2px;
  }

  .submit-btn:hover {
    transform: translateY(-1px);
    box-shadow: 0 8px 24px var(--accent-glow);
  }

  .submit-btn:active {
    transform: translateY(0);
  }

  .submit-btn:disabled {
    opacity: 0.4;
    cursor: not-allowed;
    transform: none;
    box-shadow: none;
  }

  .submit-btn .btn-text {
    transition: opacity 0.2s ease;
  }

  .submit-btn .btn-spinner {
    position: absolute;
    top: 50%;
    left: 50%;
    transform: translate(-50%, -50%);
    opacity: 0;
    transition: opacity 0.2s ease;
  }

  .submit-btn.loading .btn-text {
    opacity: 0;
  }

  .submit-btn.loading .btn-spinner {
    opacity: 1;
  }

  .spinner {
    width: 24px;
    height: 24px;
    border: 3px solid rgba(255,255,255,0.3);
    border-top-color: #fff;
    border-radius: 50%;
    animation: spin 0.7s linear infinite;
  }

  @keyframes spin {
    to { transform: rotate(360deg); }
  }

  /* Success overlay */
  .success-overlay {
    position: fixed;
    top: 0; left: 0; right: 0; bottom: 0;
    background: var(--bg-primary);
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    z-index: 100;
    opacity: 0;
    pointer-events: none;
    transition: opacity 0.4s ease;
  }

  .success-overlay.visible {
    opacity: 1;
    pointer-events: auto;
  }

  .success-checkmark {
    width: 80px;
    height: 80px;
    border-radius: 50%;
    background: linear-gradient(135deg, var(--accent), #2e7d32);
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 40px;
    animation: popIn 0.4s cubic-bezier(0.175, 0.885, 0.32, 1.275);
    box-shadow: 0 12px 40px var(--accent-glow);
  }

  @keyframes popIn {
    0% { transform: scale(0); }
    100% { transform: scale(1); }
  }

  .success-text {
    margin-top: 20px;
    font-size: 18px;
    font-weight: 600;
    color: var(--text-primary);
    animation: fadeUp 0.4s ease 0.2s both;
  }

  .success-subtext {
    margin-top: 8px;
    font-size: 14px;
    color: var(--text-secondary);
    animation: fadeUp 0.4s ease 0.35s both;
  }

  @keyframes fadeUp {
    from { opacity: 0; transform: translateY(10px); }
    to   { opacity: 1; transform: translateY(0); }
  }

  /* Footer */
  .footer {
    text-align: center;
    padding: 24px 0 8px;
    font-size: 12px;
    color: var(--text-muted);
  }
</style>
</head>
<body>

<div class="container">
  <div class="header">
    <div class="header-logo">&#x1F50D;</div>
    <h1>Avito Monitor</h1>
    <p>Мгновенные уведомления о новых объявлениях</p>
  </div>

  <div class="steps">
    <div class="step">
      <div class="step-num">1</div>
      <div class="step-text"><strong>Откройте Авито</strong> и настройте поиск: город, категория, цена, фильтры</div>
    </div>
    <div class="step">
      <div class="step-num">2</div>
      <div class="step-text"><strong>Скопируйте ссылку</strong> из адресной строки браузера</div>
    </div>
    <div class="step">
      <div class="step-num">3</div>
      <div class="step-text"><strong>Вставьте ссылку</strong> в поле ниже и нажмите кнопку</div>
    </div>
  </div>

  <div class="input-card" id="inputCard">
    <label class="input-label" for="urlInput">Ссылка на поиск Авито</label>
    <textarea
      id="urlInput"
      class="url-input"
      placeholder="https://www.avito.ru/moskva/kvartiry?f=..."
      autocomplete="off"
      autocorrect="off"
      autocapitalize="off"
      spellcheck="false"
    ></textarea>
    <div class="feedback" id="feedback"></div>
    <div class="parsed-info" id="parsedInfo"></div>
  </div>

  <button class="submit-btn" id="submitBtn" disabled>
    <span class="btn-text">Добавить отслеживание</span>
    <span class="btn-spinner"><div class="spinner"></div></span>
  </button>

  <div class="footer">Avito Monitor Bot</div>
</div>

<div class="success-overlay" id="successOverlay">
  <div class="success-checkmark">&#x2713;</div>
  <div class="success-text">Отслеживание добавлено!</div>
  <div class="success-subtext">Новые объявления придут в чат</div>
</div>

<script>
(function() {
  'use strict';

  // --- Telegram WebApp init ---
  const tg = window.Telegram && window.Telegram.WebApp;
  if (tg) {
    tg.ready();
    tg.expand();

    // Apply Telegram theme if available
    const tp = tg.themeParams;
    if (tp) {
      const root = document.documentElement.style;
      if (tp.bg_color) root.setProperty('--bg-primary', tp.bg_color);
      if (tp.secondary_bg_color) root.setProperty('--bg-card', tp.secondary_bg_color);
      if (tp.text_color) root.setProperty('--text-primary', tp.text_color);
      if (tp.hint_color) root.setProperty('--text-secondary', tp.hint_color);
      if (tp.button_color) {
        root.setProperty('--accent', tp.button_color);
        root.setProperty('--accent-glow', tp.button_color + '40');
      }
      if (tp.button_text_color) {
        // button text color available
      }
    }
  }

  // --- DOM refs ---
  const inputCard  = document.getElementById('inputCard');
  const urlInput   = document.getElementById('urlInput');
  const feedback   = document.getElementById('feedback');
  const parsedInfo = document.getElementById('parsedInfo');
  const submitBtn  = document.getElementById('submitBtn');
  const overlay    = document.getElementById('successOverlay');

  const AVITO_RE = /^https?:\/\/(?:www\.|m\.)?avito\.ru\/\S+/i;
  const KEEP_PARAMS = new Set(['f', 'q', 'pmin', 'pmax', 's', 'user', 'bt', 'cd']);

  let cleanedUrl = '';
  let isValid = false;

  // --- Focus / blur styling ---
  urlInput.addEventListener('focus', () => inputCard.classList.add('focused'));
  urlInput.addEventListener('blur',  () => inputCard.classList.remove('focused'));

  // --- Real-time validation on input ---
  let debounceTimer;
  urlInput.addEventListener('input', () => {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(validateUrl, 300);
  });

  // Also validate on paste immediately
  urlInput.addEventListener('paste', () => {
    setTimeout(validateUrl, 50);
  });

  function validateUrl() {
    const raw = urlInput.value.trim();
    const items = [];
    const tags = [];

    inputCard.classList.remove('has-error', 'has-success');
    feedback.classList.remove('visible');
    parsedInfo.classList.remove('visible');
    isValid = false;
    cleanedUrl = '';
    submitBtn.disabled = true;

    if (!raw) {
      feedback.innerHTML = '';
      parsedInfo.innerHTML = '';
      return;
    }

    // Fix Telegram mangling: replace ~ with -
    let url = raw.replace(/~/g, '-');

    // Check if it's an Avito URL
    if (!AVITO_RE.test(url)) {
      items.push(feedbackItem('error', 'Это не ссылка на Авито'));
      showFeedback(items, []);
      inputCard.classList.add('has-error');
      return;
    }

    items.push(feedbackItem('success', 'Ссылка на Авито распознана'));

    // Parse URL
    let parsed;
    try {
      parsed = new URL(url);
    } catch {
      items.push(feedbackItem('error', 'Некорректный формат URL'));
      showFeedback(items, []);
      inputCard.classList.add('has-error');
      return;
    }

    // Extract category from path
    const pathParts = parsed.pathname.split('/').filter(Boolean);
    if (pathParts.length >= 2) {
      const city = decodeURIComponent(pathParts[0]);
      const category = decodeURIComponent(pathParts[1]);
      tags.push({ icon: '&#x1F4CD;', text: city });
      tags.push({ icon: '&#x1F4C1;', text: category });
    }

    // Clean params
    const params = new URLSearchParams(parsed.search);
    const cleanParams = new URLSearchParams();
    for (const key of KEEP_PARAMS) {
      if (params.has(key)) {
        cleanParams.set(key, params.get(key));
      }
    }

    // Check f= parameter
    const fVal = params.get('f');
    if (!fVal) {
      items.push(feedbackItem('warning', 'Фильтры не обнаружены — будут показаны все объявления по ссылке'));
    } else {
      // Validate f= is complete (base64 decode, must end with })
      try {
        let padded = fVal;
        const mod = padded.length % 4;
        if (mod) padded += '='.repeat(4 - mod);
        const raw = atob(padded.replace(/-/g, '+').replace(/_/g, '/'));
        if (!raw.trimEnd().endsWith('}')) {
          items.push(feedbackItem('warning', 'Фильтры могут быть неполными — но попробуем'));
        } else {
          items.push(feedbackItem('success', 'Фильтры корректны'));
        }

        // Try to extract price info from JSON
        try {
          // f= might have a prefix byte, try to find JSON start
          const jsonStart = raw.indexOf('{');
          if (jsonStart >= 0) {
            const jsonStr = raw.substring(jsonStart);
            const fObj = JSON.parse(jsonStr);
            // Look for price keys in various formats
            if (fObj && typeof fObj === 'object') {
              for (const [k, v] of Object.entries(fObj)) {
                if (v && typeof v === 'object' && v.from !== undefined) {
                  tags.push({ icon: '&#x1F4B0;', text: 'от ' + Number(v.from).toLocaleString('ru') + ' ₽' });
                }
                if (v && typeof v === 'object' && v.to !== undefined) {
                  tags.push({ icon: '&#x1F4B0;', text: 'до ' + Number(v.to).toLocaleString('ru') + ' ₽' });
                }
              }
            }
          }
        } catch {
          // JSON parse failed — ok, filters are still valid
        }
      } catch {
        items.push(feedbackItem('warning', 'Не удалось проверить фильтры — но URL сохранён'));
      }
    }

    // Check pmin/pmax
    if (params.has('pmin')) tags.push({ icon: '&#x1F4B0;', text: 'от ' + Number(params.get('pmin')).toLocaleString('ru') + ' ₽' });
    if (params.has('pmax')) tags.push({ icon: '&#x1F4B0;', text: 'до ' + Number(params.get('pmax')).toLocaleString('ru') + ' ₽' });
    if (params.has('q'))    tags.push({ icon: '&#x1F50E;', text: params.get('q') });

    // Build cleaned URL
    const cleanQuery = cleanParams.toString();
    cleanedUrl = parsed.origin + parsed.pathname;
    if (cleanQuery) cleanedUrl += '?' + cleanQuery;

    isValid = true;
    submitBtn.disabled = false;
    inputCard.classList.add('has-success');
    showFeedback(items, tags);
  }

  function feedbackItem(type, text) {
    const icons = { success: '&#x2705;', error: '&#x274C;', warning: '&#x26A0;' };
    return '<div class="feedback-item ' + type + '">' +
      '<span class="feedback-icon">' + icons[type] + '</span>' +
      '<span>' + text + '</span></div>';
  }

  function showFeedback(items, tags) {
    feedback.innerHTML = items.join('');
    feedback.classList.toggle('visible', items.length > 0);

    if (tags.length > 0) {
      parsedInfo.innerHTML = tags.map(function(t) {
        return '<span class="parsed-tag"><span class="tag-icon">' + t.icon + '</span>' + t.text + '</span>';
      }).join('');
      parsedInfo.classList.add('visible');
    } else {
      parsedInfo.innerHTML = '';
      parsedInfo.classList.remove('visible');
    }
  }

  // --- Submit ---
  submitBtn.addEventListener('click', function() {
    if (!isValid || !cleanedUrl) return;

    submitBtn.classList.add('loading');
    submitBtn.disabled = true;

    setTimeout(function() {
      var sent = false;
      try {
        if (tg && tg.sendData) {
          tg.sendData(JSON.stringify({ url: cleanedUrl }));
          sent = true;
        }
      } catch (e) {
        // sendData failed — fall through to fallback
      }

      if (sent) {
        overlay.classList.add('visible');
        setTimeout(function() { if (tg) tg.close(); }, 1800);
      } else {
        // Fallback: copy URL to clipboard and show instructions
        submitBtn.classList.remove('loading');
        submitBtn.disabled = false;

        if (navigator.clipboard) {
          navigator.clipboard.writeText(cleanedUrl).then(function() {
            showFallbackMsg('Ссылка скопирована! Вставьте её в чат бота.');
          }).catch(function() {
            showFallbackMsg('Отправьте эту ссылку боту в чат:\\n' + cleanedUrl);
          });
        } else {
          showFallbackMsg('Отправьте эту ссылку боту в чат:\\n' + cleanedUrl);
        }

        // Close webapp after delay so user sees the message
        setTimeout(function() { if (tg) tg.close(); }, 3000);
      }
    }, 400);
  });

  function showFallbackMsg(text) {
    var el = document.createElement('div');
    el.style.cssText = 'position:fixed;top:0;left:0;right:0;bottom:0;background:var(--bg-primary);display:flex;flex-direction:column;align-items:center;justify-content:center;z-index:100;padding:24px;text-align:center;';
    el.innerHTML = '<div style="font-size:40px;margin-bottom:16px">&#x1F4CB;</div>' +
      '<div style="font-size:16px;font-weight:600;color:var(--text-primary);line-height:1.6;white-space:pre-line">' + text + '</div>';
    document.body.appendChild(el);
  }

})();
</script>
</body>
</html>
`;
    return new Response(html, {
      headers: {
        "Content-Type": "text/html;charset=UTF-8",
        "Access-Control-Allow-Origin": "*",
      },
    });
  },
};
