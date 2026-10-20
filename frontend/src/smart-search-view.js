// SPDX-FileCopyrightText: Milan Pikula
// SPDX-License-Identifier: EUPL-1.2

export function matchingSources(search) {
  const cited = new Set((search.result?.sources || [])
    .filter((source) => source.search_relevance === "supporting").map((source) => Number(source.id)));
  const rows = new Map();
  for (const source of [...(search.matching_sources || []), ...(search.result?.sources || [])]) {
    if (!cited.has(Number(source.id))) rows.set(Number(source.id), source);
  }
  return [...rows.values()];
}

export function searchFailureMessage(search) {
  const count = (search.matching_sources || []).length;
  if (count) return `Found ${count} matching email${count === 1 ? "" : "s"}, but couldn’t finish the summary. You can still read them below.`;
  if (search.error_code === "llm_paused") return "Summaries are paused. Resume them and try again.";
  return "Couldn’t finish this search. Please try again.";
}
