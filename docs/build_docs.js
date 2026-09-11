/* Build the two Word guides from their markdown sources.
 *
 *     node docs/build_docs.js
 *
 * Keeping the guides in markdown and generating the .docx means the two can
 * never drift apart: edit the markdown, run this, and both are current.
 */
const fs = require("fs");
const path = require("path");
const {
  AlignmentType, BorderStyle, Document, HeadingLevel, LevelFormat, Packer,
  Paragraph, ShadingType, Table, TableCell, TableRow, TextRun, WidthType,
} = require("docx");

const INK = "1B2A2E";
const ACCENT = "0F6E64";
const MUTED = "5B6B6E";
const CODE_BG = "F2F5F4";
const NOTE_BG = "EAF4F2";
const RULE = "D8E0DE";
const PAGE_WIDTH = 9026;            // A4 minus 1" margins, in DXA

/* ---------------------------------------------------------------- inline */
function runs(text, { size = 21, color = INK, bold = false, italics = false } = {}) {
  /* Inline markdown, recursively: **bold**, *italic*, `code` and [label](url)
   * nest inside one another (`**\`windows-start.bat\`**` is common in these
   * guides), so a single flat pass leaves stray asterisks and backticks on the
   * page. Each match recurses with the style it adds. */
  const out = [];
  const pattern = /(`[^`]+`)|(\*\*[^*]+?\*\*)|(\*[^*\n]+?\*)|(\[[^\]]+\]\([^)]+\))/;
  let rest = text;
  const plain = (value) => {
    if (value) out.push(new TextRun({ text: value, size, color, bold, italics }));
  };
  let match = pattern.exec(rest);
  while (match) {
    plain(rest.slice(0, match.index));
    const token = match[0];
    if (token.startsWith("`")) {
      out.push(new TextRun({
        text: token.slice(1, -1), font: "Consolas", size: size - 2,
        color: ACCENT, bold, italics,
        shading: { type: ShadingType.CLEAR, fill: CODE_BG },
      }));
    } else if (token.startsWith("**")) {
      out.push(...runs(token.slice(2, -2), { size, color, bold: true, italics }));
    } else if (token.startsWith("*")) {
      out.push(...runs(token.slice(1, -1), { size, color, bold, italics: true }));
    } else {
      out.push(...runs(token.slice(1, token.indexOf("]")),
                       { size, color: ACCENT, bold, italics }));
    }
    rest = rest.slice(match.index + token.length);
    match = pattern.exec(rest);
  }
  plain(rest);
  return out.length ? out : [new TextRun({ text: "", size })];
}

/* ---------------------------------------------------------------- blocks */
const para = (text, opts = {}) => new Paragraph({
  children: runs(text, opts),
  spacing: { after: opts.after === undefined ? 140 : opts.after, line: 288 },
  ...(opts.paragraph || {}),
});

const heading = (text, level) => new Paragraph({
  children: runs(text.replace(/^#+\s*/, ""),
                 { size: level === 1 ? 40 : level === 2 ? 28 : 24, bold: true,
                   color: level === 1 ? INK : ACCENT }),
  heading: level === 1 ? HeadingLevel.HEADING_1
         : level === 2 ? HeadingLevel.HEADING_2 : HeadingLevel.HEADING_3,
  spacing: { before: level === 1 ? 0 : 300, after: level === 1 ? 200 : 120 },
  ...(level === 2 ? {
    border: { bottom: { style: BorderStyle.SINGLE, size: 6, color: RULE, space: 6 } },
  } : {}),
});

const codeBlock = (lines) => lines.map((line, index) => new Paragraph({
  children: [new TextRun({ text: line || " ", font: "Consolas", size: 19, color: INK })],
  shading: { type: ShadingType.CLEAR, fill: CODE_BG },
  spacing: { before: index === 0 ? 60 : 0, after: index === lines.length - 1 ? 160 : 0,
             line: 264 },
  indent: { left: 220, right: 220 },
  border: { left: { style: BorderStyle.SINGLE, size: 12, color: ACCENT, space: 8 } },
}));

const quote = (lines) => lines.map((line, index) => new Paragraph({
  children: runs(line, { size: 20, color: INK }),
  shading: { type: ShadingType.CLEAR, fill: NOTE_BG },
  spacing: { before: index === 0 ? 80 : 0, after: index === lines.length - 1 ? 170 : 40,
             line: 276 },
  indent: { left: 220, right: 220 },
}));

const bullet = (text) => new Paragraph({
  children: runs(text), bullet: { level: 0 }, spacing: { after: 80, line: 288 },
});

const numbered = (text, ref) => new Paragraph({
  children: runs(text), numbering: { reference: ref, level: 0 },
  spacing: { after: 80, line: 288 },
});

function table(rows) {
  const columns = rows[0].length;
  const width = Math.floor(PAGE_WIDTH / columns);
  const widths = Array.from({ length: columns }, (_, i) =>
    i === columns - 1 ? PAGE_WIDTH - width * (columns - 1) : width);
  return new Table({
    columnWidths: widths,
    width: { size: PAGE_WIDTH, type: WidthType.DXA },
    rows: rows.map((cells, rowIndex) => new TableRow({
      tableHeader: rowIndex === 0,
      // A row broken across a page break loses its left-hand cell entirely at
      // the top of the next page, which reads as a blank first column.
      cantSplit: true,
      children: cells.map((cell, columnIndex) => new TableCell({
        width: { size: widths[columnIndex], type: WidthType.DXA },
        shading: { type: ShadingType.CLEAR, fill: rowIndex === 0 ? NOTE_BG : "FFFFFF" },
        margins: { top: 80, bottom: 80, left: 120, right: 120 },
        children: [new Paragraph({
          children: runs(cell, { size: 20, bold: rowIndex === 0 }),
          spacing: { after: 0, line: 264 },
        })],
      })),
    })),
  });
}

/* ---------------------------------------------------------------- parser */
function convert(markdown, numberingRefs) {
  const lines = markdown.split("\n");
  const children = [];
  let i = 0;
  let listIndex = 0;

  while (i < lines.length) {
    const line = lines[i];

    if (/^```/.test(line)) {
      const body = [];
      i += 1;
      while (i < lines.length && !/^```/.test(lines[i])) body.push(lines[i++]);
      i += 1;
      children.push(...codeBlock(body));
      continue;
    }
    if (/^#{1,3} /.test(line)) {
      children.push(heading(line, (line.match(/^#+/) || ["#"])[0].length));
      i += 1;
      continue;
    }
    if (/^---+\s*$/.test(line)) {
      children.push(new Paragraph({
        text: "", spacing: { after: 120 },
        border: { bottom: { style: BorderStyle.SINGLE, size: 6, color: RULE, space: 4 } },
      }));
      i += 1;
      continue;
    }
    if (/^> ?/.test(line)) {
      const body = [];
      while (i < lines.length && /^>/.test(lines[i])) {
        body.push(lines[i].replace(/^> ?/, ""));
        i += 1;
      }
      // A blockquote's own wrapped lines belong to one paragraph.
      const merged = [];
      body.forEach((part) => {
        if (part.trim() === "") merged.push("");
        else if (merged.length && merged[merged.length - 1] !== "") {
          merged[merged.length - 1] += " " + part.trim();
        } else merged.push(part.trim());
      });
      children.push(...quote(merged.filter((p) => p !== "")));
      continue;
    }
    if (/^\|/.test(line)) {
      const rows = [];
      while (i < lines.length && /^\|/.test(lines[i])) {
        const cells = lines[i].split("|").slice(1, -1).map((c) => c.trim());
        if (!cells.every((c) => /^:?-+:?$/.test(c))) rows.push(cells);
        i += 1;
      }
      children.push(table(rows));
      children.push(new Paragraph({ text: "", spacing: { after: 160 } }));
      continue;
    }
    if (/^\s*[*-] /.test(line)) {
      let text = line.replace(/^\s*[*-] /, "").trim();
      i += 1;
      while (i < lines.length && /^\s+\S/.test(lines[i]) && !/^\s*[*-] /.test(lines[i])
             && !/^\s*\d+\. /.test(lines[i])) {
        text += " " + lines[i].trim();
        i += 1;
      }
      children.push(bullet(text));
      continue;
    }
    if (/^\s*\d+\. /.test(line)) {
      const ref = `list-${listIndex}`;
      numberingRefs.push(ref);
      while (i < lines.length && (/^\s*\d+\. /.test(lines[i]) || /^\s+\S/.test(lines[i]))) {
        if (/^\s*\d+\. /.test(lines[i])) {
          let text = lines[i].replace(/^\s*\d+\. /, "").trim();
          i += 1;
          while (i < lines.length && /^\s+\S/.test(lines[i]) && !/^\s*\d+\. /.test(lines[i])) {
            text += " " + lines[i].trim();
            i += 1;
          }
          children.push(numbered(text, ref));
        } else i += 1;
      }
      listIndex += 1;
      continue;
    }
    if (line.trim() === "") {
      i += 1;
      continue;
    }
    // An ordinary paragraph, gathering its wrapped lines.
    let text = line.trim();
    i += 1;
    while (i < lines.length && lines[i].trim() !== "" && !/^[#>|`-]/.test(lines[i])
           && !/^\s*[*-] /.test(lines[i]) && !/^\s*\d+\. /.test(lines[i])) {
      text += " " + lines[i].trim();
      i += 1;
    }
    children.push(para(text));
  }
  return children;
}

function build(source, target, title) {
  const refs = [];
  const children = convert(fs.readFileSync(source, "utf8"), refs);
  const doc = new Document({
    creator: "ReverseBid",
    title,
    numbering: {
      config: refs.map((reference) => ({
        reference,
        levels: [{
          level: 0, format: LevelFormat.DECIMAL, text: "%1.",
          alignment: AlignmentType.START,
          style: { paragraph: { indent: { left: 520, hanging: 280 } } },
        }],
      })),
    },
    styles: {
      default: { document: { run: { font: "Calibri", size: 21, color: INK } } },
    },
    sections: [{
      properties: { page: { margin: { top: 1080, bottom: 1080, left: 1080, right: 1080 } } },
      children,
    }],
  });
  return Packer.toBuffer(doc).then((buffer) => {
    fs.writeFileSync(target, buffer);
    console.log("wrote", path.basename(target), buffer.length, "bytes");
  });
}

const here = __dirname;
build(path.join(here, "RUNNING-ON-WINDOWS.md"),
      path.join(here, "1 - Running ReverseBid on Windows.docx"),
      "Running ReverseBid on Windows")
  .then(() => build(path.join(here, "PUTTING-IT-ON-GITHUB.md"),
                    path.join(here, "2 - Putting ReverseBid on GitHub.docx"),
                    "Putting ReverseBid on GitHub"))
  .then(() => build(path.join(here, "PUTTING-IT-ONLINE.md"),
                    path.join(here, "3 - Putting ReverseBid online.docx"),
                    "Putting ReverseBid online"))
  .catch((err) => { console.error(err); process.exit(1); });
