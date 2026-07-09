import type { ReactNode } from "react";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

export function Tip({
  tip,
  children,
  side = "top",
}: {
  tip: string;
  children: ReactNode;
  side?: "top" | "right" | "bottom" | "left";
}) {
  const i = tip.indexOf("|");
  const title = i >= 0 ? tip.slice(0, i) : tip;
  const body = i >= 0 ? tip.slice(i + 1) : "";
  return (
    <Tooltip>
      <TooltipTrigger asChild>{children}</TooltipTrigger>
      <TooltipContent side={side} className="max-w-[19rem]">
        <span className="text-[12.5px] font-semibold leading-tight">{title}</span>
        {body && (
          <span className="mt-0.5 text-[11.5px] leading-snug text-muted-foreground">{body}</span>
        )}
      </TooltipContent>
    </Tooltip>
  );
}
