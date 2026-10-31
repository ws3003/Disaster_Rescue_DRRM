// Presentation-only selection from the loaded recent groups. Never compare cross-group times.
function markLatestByCategory(views) {
  const seen = Object.create(null);
  return views.map(function (view) {
    const latest = !seen[view.category];
    seen[view.category] = true;
    return Object.assign({}, view, {
      latestLabel: latest ? (view.category === 'person' ? '人员最新' : '传感器最新') : ''
    });
  });
}
function buildHomeFocus(views) {
  const marked = markLatestByCategory(views);
  const score = { high: 3, medium: 2, low: 1 };
  // Only select candidates; rendering preserves the original group order.
  const candidates = marked.map(function (view, index) { return { index: index, score: score[view.severityKey] || 0 }; });
  candidates.sort(function (a, b) { return b.score - a.score || a.index - b.index; });
  const chosen = candidates.slice(0, 2).map(function (entry) { return entry.index; });
  return {
    focus: marked.filter(function (_, index) { return chosen.indexOf(index) !== -1; }),
    remaining: marked.filter(function (_, index) { return chosen.indexOf(index) === -1; })
  };
}
module.exports = { buildHomeFocus, markLatestByCategory };
