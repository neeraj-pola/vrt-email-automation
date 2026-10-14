import { api } from "./api";
import type { Schedule } from "./types";

export interface PipelineConfig {
  follow_up_wait_days: number;
  max_follow_ups: number;
  daily_send_cap: number;
}

// Fetched once per session and cached -- these are server-side settings
// (§8), not something that changes while the dashboard is open.
let cached: Promise<PipelineConfig> | null = null;

export function getPipelineConfig(): Promise<PipelineConfig> {
  if (!cached) {
    cached = api.getPipelineConfig().catch((e) => {
      cached = null; // let a later call retry instead of caching a failure
      throw e;
    });
  }
  return cached;
}

// The follow-up schedule is staff-editable (unlike the settings above), so
// it's cached but replaced the moment it's saved -- anything that mounts
// afterwards (e.g. a lead panel) sees the new values without a reload.
let cachedSchedule: Promise<Schedule> | null = null;

export function getSchedule(): Promise<Schedule> {
  if (!cachedSchedule) {
    cachedSchedule = api.getSchedule().catch((e) => {
      cachedSchedule = null;
      throw e;
    });
  }
  return cachedSchedule;
}

export function setCachedSchedule(schedule: Schedule): void {
  cachedSchedule = Promise.resolve(schedule);
}
