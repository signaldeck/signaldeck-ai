/* Render model-authored Markdown without permitting raw HTML or remote images. */
(function (global) {
  const parser = typeof global.markdownit === "function" ? global.markdownit({
    html: false,
    linkify: true,
    breaks: true,
    typographer: false,
  }) : null;

  if (parser) {
    // The chat must not automatically load model-provided image URLs.
    parser.disable("image");
    parser.renderer.rules.link_open = function (tokens, index, options, env, renderer) {
      tokens[index].attrSet("target", "_blank");
      tokens[index].attrSet("rel", "noopener noreferrer");
      return renderer.renderToken(tokens, index, options);
    };
  }

  global.signaldeckAIMarkdown = {
    render: function (source) {
      // null signals that the Markdown library is missing; callers show plain text.
      return parser ? parser.render(String(source ?? "")) : null;
    },
  };
})(window);
