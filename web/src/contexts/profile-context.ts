import { createContext } from "react";

import type { ProfileManagementMode } from "@/lib/api";

export interface ProfileContextValue {
  /** Profile every management surface reads/writes ("" = the dashboard
   *  process's own profile). */
  profile: string;
  /** The profile the dashboard process itself runs under. */
  currentProfile: string;
  /** Known profile names (includes "default"). */
  profiles: string[];
  /** Whether the dashboard manages host profiles or one authenticated owner. */
  managementMode: ProfileManagementMode;
  setProfile: (name: string) => void;
}

export const ProfileContext = createContext<ProfileContextValue>({
  profile: "",
  currentProfile: "default",
  profiles: [],
  managementMode: "legacy_multi_profile",
  setProfile: () => {},
});
