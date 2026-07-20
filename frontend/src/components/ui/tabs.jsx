import * as TabsPrimitive from "@radix-ui/react-tabs";
import { cn } from "../../lib/utils";

export function Tabs({ className, ...props }) {
  return <TabsPrimitive.Root className={cn("w-full", className)} {...props} />;
}

export function TabsList({ className, ...props }) {
  return (
    <TabsPrimitive.List
      className={cn("inline-flex rounded-lg border border-slate-200 bg-slate-100 p-1 dark:border-slate-800 dark:bg-slate-900", className)}
      {...props}
    />
  );
}

export function TabsTrigger({ className, ...props }) {
  return (
    <TabsPrimitive.Trigger
      className={cn(
        "rounded-md px-3 py-1.5 text-sm text-slate-600 data-[state=active]:bg-white data-[state=active]:text-slate-900 dark:text-slate-300 dark:data-[state=active]:bg-slate-100 dark:data-[state=active]:text-slate-900",
        className
      )}
      {...props}
    />
  );
}

export function TabsContent({ className, ...props }) {
  return <TabsPrimitive.Content className={cn("mt-3", className)} {...props} />;
}
