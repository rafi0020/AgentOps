/** A word-level diff of two texts (longest common subsequence), for comparing prompt versions. */
export type Piece = { kind: "same" | "add" | "remove"; text: string };

export function diffWords(a: string, b: string): Piece[] {
  const x = a.split(/(\s+)/), y = b.split(/(\s+)/);
  if (x.length * y.length > 4_000_000) return [{ kind: "remove", text: a }, { kind: "add", text: b }];
  const table = Array.from({ length: x.length + 1 }, () => new Uint32Array(y.length + 1));
  for (let i = x.length - 1; i >= 0; i--) for (let j = y.length - 1; j >= 0; j--) table[i][j] = x[i] === y[j] ? table[i + 1][j + 1] + 1 : Math.max(table[i + 1][j], table[i][j + 1]);
  const out: Piece[] = [];
  const push = (kind: Piece["kind"], text: string) => { const last = out[out.length - 1]; if (last?.kind === kind) last.text += text; else out.push({ kind, text }); };
  let i = 0, j = 0;
  while (i < x.length && j < y.length) {
    if (x[i] === y[j]) { push("same", x[i]); i++; j++; }
    else if (table[i + 1][j] >= table[i][j + 1]) push("remove", x[i++]);
    else push("add", y[j++]);
  }
  while (i < x.length) push("remove", x[i++]);
  while (j < y.length) push("add", y[j++]);
  return out;
}

/** A line-level diff of two texts (longest common subsequence): one piece per line, in order. */
export function diffLines(a: string, b: string): Piece[] {
  const x = a.split("\n"), y = b.split("\n");
  if (x.length * y.length > 4_000_000) return [...x.map((text) => ({ kind: "remove" as const, text })), ...y.map((text) => ({ kind: "add" as const, text }))];
  const table = Array.from({ length: x.length + 1 }, () => new Uint32Array(y.length + 1));
  for (let i = x.length - 1; i >= 0; i--) for (let j = y.length - 1; j >= 0; j--) table[i][j] = x[i] === y[j] ? table[i + 1][j + 1] + 1 : Math.max(table[i + 1][j], table[i][j + 1]);
  const out: Piece[] = [];
  let i = 0, j = 0;
  while (i < x.length && j < y.length) {
    if (x[i] === y[j]) { out.push({ kind: "same", text: x[i] }); i++; j++; }
    else if (table[i + 1][j] >= table[i][j + 1]) out.push({ kind: "remove", text: x[i++] });
    else out.push({ kind: "add", text: y[j++] });
  }
  while (i < x.length) out.push({ kind: "remove", text: x[i++] });
  while (j < y.length) out.push({ kind: "add", text: y[j++] });
  return out;
}
