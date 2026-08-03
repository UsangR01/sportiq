import { Modal, Pressable, Text, View } from "react-native";
import { useState } from "react";

export interface DropdownOption {
  value: string;
  label: string;
}

interface Props {
  /** Trigger text — the active option's label, or a placeholder when nothing is selected. */
  triggerLabel: string;
  options: DropdownOption[];
  selected: string | null;
  /** Called with the tapped value. Selection semantics are the CALLER's: the sport dropdown
   * treats a tap as a plain choice, while status toggles a repeat tap back to "all". */
  onSelect: (value: string) => void;
  accessibilityLabel: string;
  /** Roughly aligns the menu under its own trigger. Only two positions are needed today. */
  align?: "left" | "right";
  triggerClassName?: string;
}

/** Shared dropdown shell: pill trigger plus a tap-outside-to-dismiss menu.
 *
 * Extracted once a second dropdown (status) needed the same modal, backdrop and option-row
 * behaviour as the sport one. Deliberately holds no opinion about what selection MEANS —
 * that differs between the two and belongs with the caller. */
export function DropdownMenu({
  triggerLabel,
  options,
  selected,
  onSelect,
  accessibilityLabel,
  align = "left",
  triggerClassName = "",
}: Props) {
  const [open, setOpen] = useState(false);

  return (
    <>
      <Pressable
        onPress={() => setOpen(true)}
        accessibilityRole="button"
        accessibilityLabel={accessibilityLabel}
        className={`flex-row items-center justify-between gap-1 rounded-2xl bg-gray-100 px-4 py-3 active:opacity-70 dark:bg-gray-800 ${triggerClassName}`}
      >
        <Text className="font-semibold text-gray-900 dark:text-white" numberOfLines={1}>
          {triggerLabel}
        </Text>
        <Text className="text-xs text-gray-500 dark:text-gray-400">{open ? "▲" : "▼"}</Text>
      </Pressable>

      <Modal visible={open} transparent animationType="fade" onRequestClose={() => setOpen(false)}>
        {/* Full-screen backdrop: RN's Modal has no light-dismiss of its own. */}
        <Pressable className="flex-1" onPress={() => setOpen(false)}>
          <View
            className={`mt-32 w-56 overflow-hidden rounded-2xl bg-white shadow-lg dark:bg-gray-800 ${
              align === "right" ? "self-end mr-4" : "ml-4"
            }`}
          >
            {options.map((option) => {
              const active = selected === option.value;
              return (
                <Pressable
                  key={option.value}
                  onPress={() => {
                    onSelect(option.value);
                    setOpen(false);
                  }}
                  accessibilityRole="button"
                  accessibilityState={{ selected: active }}
                  className={`px-5 py-4 active:opacity-70 ${
                    active ? "bg-blue-50 dark:bg-blue-950" : ""
                  }`}
                >
                  <Text
                    className={
                      active
                        ? "text-base font-semibold text-blue-600 dark:text-blue-400"
                        : "text-base text-gray-900 dark:text-gray-100"
                    }
                  >
                    {option.label}
                  </Text>
                </Pressable>
              );
            })}
          </View>
        </Pressable>
      </Modal>
    </>
  );
}
