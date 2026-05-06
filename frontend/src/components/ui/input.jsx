import { cn } from "../../lib/utils";

export function Input({ className, ...props }) {
  return (
    <input
      className={cn(
        "h-11 w-full rounded-md border border-slate-700 bg-slate-800/80 px-3 text-sm text-slate-100 outline-none placeholder:text-slate-500 focus:border-slate-500",
        className
      )}
      {...props}
    />
  );
}
