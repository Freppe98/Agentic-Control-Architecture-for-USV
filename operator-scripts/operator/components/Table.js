// Table(columns, rows, opts) — reusable dense sortable table.
// columns: [{ key, label, align:'num'?, sortable?, render(row)->html }]
// rows: objects with .id. Page wires clicks (th[data-sort-key], tr[data-id]).
export function Table(columns, rows, { selectedId = null, sort = null } = {}) {
  const head = columns.map((c) => {
    const numc = c.align === "num" ? " num" : "";
    const sc = c.sortable ? " sortable" : "";
    const attr = c.sortable ? ` data-sort-key="${c.key}"` : "";
    const ar = sort && sort.key === c.key ? `<span class="ar">${sort.dir < 0 ? "▼" : "▲"}</span>` : "";
    return `<th class="${sc}${numc}"${attr}>${c.label}${ar}</th>`;
  }).join("");
  const body = rows.map((r) => {
    const cells = columns.map((c) => `<td class="${c.align === "num" ? "num" : ""}">${c.render(r)}</td>`).join("");
    return `<tr data-id="${r.id}" class="${r.id === selectedId ? "sel" : ""}">${cells}</tr>`;
  }).join("");
  return `<table class="dt"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}
