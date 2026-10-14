// Converts between the stored HTML (a few <p> blocks, occasionally <br>
// within one) and a plain-text form staff actually edit -- paragraphs
// separated by a blank line, no tags visible at all.

function stripTags(s: string): string {
  return s
    .replace(/<[^>]+>/g, "")
    .replace(/&nbsp;/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&#39;/g, "'")
    .replace(/&quot;/g, '"');
}

function escapeHtml(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

export function htmlToPlainText(html: string): string {
  const withBreaks = html.replace(/<br\s*\/?>/gi, "\n");
  const paragraphs: string[] = [];
  const re = /<p[^>]*>([\s\S]*?)<\/p>/gi;
  let match: RegExpExecArray | null;
  let found = false;
  while ((match = re.exec(withBreaks)) !== null) {
    found = true;
    paragraphs.push(stripTags(match[1]).trim());
  }
  if (!found) return stripTags(withBreaks).trim();
  return paragraphs.join("\n\n");
}

export function plainTextToHtml(text: string): string {
  const paragraphs = text
    .split(/\n\s*\n/)
    .map((p) => p.trim())
    .filter(Boolean);
  return paragraphs.map((p) => `<p>${escapeHtml(p).replace(/\n/g, "<br>")}</p>`).join("");
}
