import { describe, expect, it } from "vitest";
import { DataError, EXPORT_COMMAND, parseDashboardData } from "./parse";

function valid() {
  return {
    schema_version: 1,
    meta: { synthetic_traffic: true, replay_not_live_aws: true },
    episodes: [{ id: "e1", minutes: 3, traffic: [1, 2, 3], need: [1, 1, 1], arms: {}, forecast: {} }],
    accuracy: {},
    aws: {},
  };
}

describe("parseDashboardData", () => {
  it("accepts a well-formed file", () => {
    expect(parseDashboardData(valid()).episodes).toHaveLength(1);
  });

  it("rejects something that is not an object, and says how to regenerate the file", () => {
    expect(() => parseDashboardData([])).toThrow(DataError);
    expect(() => parseDashboardData(null)).toThrow(EXPORT_COMMAND);
  });

  it("rejects a different schema version", () => {
    const data = { ...valid(), schema_version: 2 };
    expect(() => parseDashboardData(data)).toThrow(/schema version 2/);
  });

  it("refuses data that does not declare itself a replay on synthetic traffic", () => {
    const data = { ...valid(), meta: { synthetic_traffic: true, replay_not_live_aws: false } };
    expect(() => parseDashboardData(data)).toThrow(/replay on synthetic traffic/);
    expect(() => parseDashboardData({ ...valid(), meta: undefined })).toThrow(DataError);
  });

  it("rejects an empty episode list", () => {
    expect(() => parseDashboardData({ ...valid(), episodes: [] })).toThrow(/no episodes/);
  });

  it("rejects an episode whose series do not match its length", () => {
    const data = valid();
    data.episodes[0] = { ...data.episodes[0]!, traffic: [1, 2] };
    expect(() => parseDashboardData(data)).toThrow(/traffic should have 3 values/);
  });

  it("rejects an episode with no approaches or forecast", () => {
    const data = valid();
    data.episodes[0] = { ...data.episodes[0]!, arms: undefined as never };
    expect(() => parseDashboardData(data)).toThrow(/approaches or forecast/);
  });

  it("rejects a file missing the accuracy or AWS blocks", () => {
    expect(() => parseDashboardData({ ...valid(), aws: undefined })).toThrow(/accuracy or AWS/);
  });
});
