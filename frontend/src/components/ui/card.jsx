import { cn } from "../../lib/utils";

export function Card({ className, ...props }) {
  return (
    <div
      className={cn(
        "rounded-xl border border-slate-200 bg-white text-slate-900 shadow-sm dark:border-[#7B1FFF]/30 dark:bg-[#120522]/70 dark:text-[#F4EEFF] dark:shadow-[0_10px_30px_rgba(155,53,255,0.08)]",
        className
      )}
      {...props}
    />
  );
}

export function CardHeader({ className, ...props }) {
  return <div className={cn("border-b border-slate-200 px-4 py-3 dark:border-[#7B1FFF]/20", className)} {...props} />;
}

export function CardContent({ className, ...props }) {
  return <div className={cn("p-4", className)} {...props} />;
}
