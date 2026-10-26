const test = require('node:test');
const assert = require('node:assert/strict');

global.window = {
  markdownit: require('../plugins/signaldeck_ai/static/js/markdown-it.min.js'),
};
require('../plugins/signaldeck_ai/static/js/chat_markdown.js');

const render = window.signaldeckAIMarkdown.render;

test('renders emphasis, tables, lists and fenced code blocks', () => {
  const html = render(
    '**Fett**\n\n| Zeit | Wert |\n| --- | --- |\n| 06:41 | 60 % |' +
    '\n\n- Eins\n- Zwei\n\n```python\nprint(1)\n```'
  );
  assert.match(html, /<strong>Fett<\/strong>/);
  assert.match(html, /<table>/);
  assert.match(html, /<th>Zeit<\/th>/);
  assert.match(html, /<td>60 %<\/td>/);
  assert.match(html, /<li>Eins<\/li>/);
  assert.match(html, /<code class="language-python">print\(1\)/);
});

test('escapes raw HTML and rejects unsafe links and remote images', () => {
  const html = render(
    '<script>alert(1)</script>\n\n[x](javascript:alert(1))' +
    '\n\n![tracking](https://example.test/x.png)'
  );
  assert.doesNotMatch(html, /<script|<img|href="javascript:/i);
  assert.match(html, /&lt;script&gt;/);
});

test('links open separately without access to the chat tab', () => {
  const html = render('[Quelle](https://example.test/)');
  assert.match(html, /target="_blank"/);
  assert.match(html, /rel="noopener noreferrer"/);
});
