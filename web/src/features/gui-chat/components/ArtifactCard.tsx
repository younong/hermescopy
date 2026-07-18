import type { ArtifactState } from "../types";
import { FileArtifactCard } from "./FileArtifactCard";
import { ImageArtifactCard } from "./ImageArtifactCard";

export function ArtifactCard({
  artifact,
  variant = "card",
}: {
  artifact: ArtifactState;
  variant?: "bubble" | "card";
}) {
  return artifact.kind === "file" ? (
    <FileArtifactCard artifact={artifact} variant={variant} />
  ) : (
    <ImageArtifactCard artifact={artifact} variant={variant} />
  );
}
