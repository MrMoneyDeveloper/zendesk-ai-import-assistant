import { cva } from "class-variance-authority";
import { cn } from "../../lib/utils";

const badgeVariants = cva(
  "inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-semibold",
  {
    variants: {
      variant: {
        neutral: "border-[#7B1FFF]/45 text-[#B9A7D9] bg-[#120522]/80",
        success: "border-emerald-600/50 bg-emerald-950 text-emerald-300",
        warning: "border-amber-600/50 bg-amber-950 text-amber-300",
        danger: "border-rose-600/50 bg-rose-950 text-rose-300",
      },
    },
    defaultVariants: {
      variant: "neutral",
    },
  }
);

export function Badge({ className, variant, ...props }) {
  return <span className={cn(badgeVariants({ variant, className }))} {...props} />;
}
