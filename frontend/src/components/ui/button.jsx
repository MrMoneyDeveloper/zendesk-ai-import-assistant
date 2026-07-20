import { cva } from "class-variance-authority";
import { cn } from "../../lib/utils";

const buttonVariants = cva(
  "inline-flex items-center justify-center rounded-md text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#C063FF]/50 disabled:pointer-events-none disabled:opacity-50",
  {
    variants: {
      variant: {
        default: "border border-violet-300 bg-violet-50 text-violet-700 hover:bg-violet-100 dark:border-[#7B1FFF]/35 dark:bg-[#9B35FF]/20 dark:text-[#F4EEFF] dark:hover:bg-[#9B35FF]/30",
        primary: "border border-[#7B1FFF]/45 bg-[#9B35FF] text-[#F4EEFF] hover:bg-[#C063FF]",
        ghost: "text-slate-600 hover:bg-slate-100 hover:text-slate-900 dark:text-[#B9A7D9] dark:hover:bg-[#7B1FFF]/20 dark:hover:text-[#F4EEFF]",
        outline: "border border-slate-300 bg-white text-slate-700 hover:bg-slate-50 dark:border-[#7B1FFF]/45 dark:bg-transparent dark:text-[#F4EEFF] dark:hover:bg-[#7B1FFF]/15",
      },
      size: {
        sm: "h-8 px-3 text-xs",
        md: "h-10 px-4 py-2",
        lg: "h-12 px-5 text-base",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "md",
    },
  }
);

export function Button({ className, variant, size, ...props }) {
  return <button className={cn(buttonVariants({ variant, size, className }))} {...props} />;
}
