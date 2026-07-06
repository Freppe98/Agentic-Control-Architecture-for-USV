// Table(columns, rows, opts) — reusable dense sortable table.
// columns: [{ key, label, align:'num'?, sortable?, render(row)->html }]
// rows: objects keyed by opts.idKey (default 'id'). Page wires clicks (th[data-sort-key], tr[data-id]).
// opts.rowClass(row)->string adds per-row classes (e.g. severity tint); backward compatible.
export function Table(columns, rows, { selectedId = null, sort = null, idKey = "id", rowClass = null } = {}) {
  const head = columns.map((c) => {
    const numc = c.align === "num" ? " num" : "";
    const sc = c.sortable ? " sortable" : "";
    const attr = c.sortable ? ` data-sort-key="${c.key}"` : "";
    const ar = sort && sort.key === c.key ? `<span class="ar">${sort.dir < 0 ? "▼" : "▲"}</span>` : "";
    return `<th class="${sc}${numc}"${attr}>${c.label}${ar}</th>`;
  }).join("");
  const body = rows.map((r) => {
    const id = r[idKey];
    const extra = rowClass ? rowClass(r) : "";
    const cells = columns.map((c) => `<td class="${c.align === "num" ? "num" : ""}">${c.render(r)}</td>`).join("");
    return `<tr data-id="${id}" class="${id === selectedId ? "sel" : ""}${extra ? " " + extra : ""}">${cells}</tr>`;
  }).join("");
  return `<table class="dt"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}
