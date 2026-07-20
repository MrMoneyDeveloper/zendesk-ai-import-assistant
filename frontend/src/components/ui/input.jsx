import { cn } from "../../lib/utils";

export function Input({ className, ...props }) {
  return (
    <input
      className={cn(
        "h-11 w-full rounded-md border border-slate-300 bg-white px-3 text-sm text-slate-800 outline-none placeholder:text-slate-400 focus:border-violet-500 dark:border-[#7B1FFF]/40 dark:bg-[#07030F]/70 dark:text-[#F4EEFF] dark:placeholder:text-[#B9A7D9]/65 dark:focus:border-[#C063FF]/70",
        className
      )}
      {...props}
    />
  );
}
