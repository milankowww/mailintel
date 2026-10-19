import test from "node:test";
import assert from "node:assert/strict";
import { matchingSources, searchFailureMessage } from "../src/smart-search-view.js";

test("matches remain available after failure without exposing diagnostics", () => {
  const search = { status: "failed", error_code: "compression_failed", error: "provider traceback",
    matching_sources: [{ id: 1, title: "Invitation" }, { id: 2, title: "Dates" }] };
  assert.equal(matchingSources(search).length, 2);
  const text = searchFailureMessage(search);
  assert.match(text, /Found 2 matching emails/);
  assert.doesNotMatch(text, /provider|compression|traceback/);
});

test("only cited evidence leaves the matching list; other matches survive success", () => {
  const search = { matching_sources: [{ id: 1 }, { id: 2 }, { id: 3 }],
    result: { sources: [{ id: 1, search_relevance: "supporting" }, { id: 2, search_relevance: "maybe_relevant" }] } };
  assert.deepEqual(matchingSources(search).map((s) => s.id), [2, 3]);
  assert.equal(matchingSources({ ...search, result: {}, status: "cancelled" }).length, 3);
});
