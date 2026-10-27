import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { JobResults } from "@/components/research/job-results";
import type { JobKind } from "@/lib/research-types";

describe("recorded holdout exposure", () => {
  it.each<JobKind>(["train", "predict", "backtest"])("shows %s reuse without requiring JSON inspection", (kind) => {
    render(<JobResults kind={kind} result={{ validation: { holdout_reuse: { holdout_reused: true, prior_completed_attempts: 2 } } }} />);
    expect(screen.getByRole("status").textContent).toContain("Holdout reused");
    expect(screen.getByRole("status").textContent).toContain("not an independent evaluation");
  });

  it("does not certify a fresh holdout when no exact repeat was found", () => {
    render(<JobResults kind="train" result={{ validation: { holdout_reuse: { holdout_reused: false, prior_completed_attempts: 0 } } }} />);
    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.queryByText(/fresh|independent holdout/i)).toBeNull();
  });
});
