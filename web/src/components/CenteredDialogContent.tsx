import type { ComponentProps } from "react";
import { DialogContent } from "@nous-research/ui/ui/components/dialog";
import { cn } from "@/lib/utils";

export function CenteredDialogContent({
  className,
  ...props
}: ComponentProps<typeof DialogContent>) {
  return (
    <DialogContent
      className={cn(
        "[transform:translate(-50%,-50%)] [translate:none]",
        className,
      )}
      {...props}
    />
  );
}
