/* Render model-authored Markdown without permitting raw HTML or remote images. */
(function (global) {
  let parser = null;

  function getParser() {
    // Scripts may arrive in a different order. Retry on the next render if
    // markdown-it was not available when this module first loaded.
    if (parser || typeof global.markdownit !== "function") return parser;
    parser = global.markdownit({
      html: false,
      linkify: true,
      breaks: true,
      typographer: false,
    });
    // The chat must not automatically load model-provided image URLs.
    parser.disable("image");
    parser.renderer.rules.link_open = function (tokens, index, options, env, renderer) {
      tokens[index].attrSet("target", "_blank");
      tokens[index].attrSet("rel", "noopener noreferrer");
      return renderer.renderToken(tokens, index, options);
    };
    return parser;
  }

  global.signaldeckAIMarkdown = {
    render: function (source) {
      // null signals that the Markdown library is missing; callers show plain text.
      const markdown = getParser();
      return markdown ? markdown.render(String(source ?? "")) : null;
    },
  };
})(window);
