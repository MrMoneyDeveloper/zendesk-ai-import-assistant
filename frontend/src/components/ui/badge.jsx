import { cva } from "class-variance-authority";
import { cn } from "../../lib/utils";

const badgeVariants = cva(
  "inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-semibold",
  {
    variants: {
      variant: {
        neutral: "border-slate-300 bg-slate-100 text-slate-600 dark:border-[#7B1FFF]/45 dark:bg-[#120522]/80 dark:text-[#B9A7D9]",
        success: "border-emerald-300 bg-emerald-50 text-emerald-700 dark:border-emerald-600/50 dark:bg-emerald-950 dark:text-emerald-300",
        warning: "border-amber-300 bg-amber-50 text-amber-800 dark:border-amber-600/50 dark:bg-amber-950 dark:text-amber-300",
        danger: "border-rose-300 bg-rose-50 text-rose-700 dark:border-rose-600/50 dark:bg-rose-950 dark:text-rose-300",
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
