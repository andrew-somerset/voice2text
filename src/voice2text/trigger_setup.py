"""Small first-run trigger picker used by an installer or explicit setup command."""

from __future__ import annotations

from voice2text.trigger_settings import trigger_choice, trigger_choices


class TriggerSetupError(RuntimeError):
    """The first-run trigger picker could not be displayed."""


def choose_trigger(initial_choice_id: str = "right-ctrl") -> str | None:
    """Show a focused setup dialog and return a reviewed choice ID, or None on cancel."""

    initial_choice = trigger_choice(initial_choice_id)
    try:
        import tkinter as tk
        from tkinter import ttk
    except ImportError:
        raise TriggerSetupError("The Windows trigger setup UI is unavailable") from None

    selected_choice: list[str | None] = [None]
    try:
        root = tk.Tk()
    except tk.TclError:
        raise TriggerSetupError("The Windows trigger setup UI could not be opened") from None

    root.title("Choose your voice trigger")
    root.resizable(False, False)
    root.configure(background="#f3f4f6")

    shell = tk.Frame(root, background="#f3f4f6", padx=24, pady=20)
    shell.pack(fill="both", expand=True)
    tk.Label(
        shell,
        text="Choose your voice trigger",
        background="#f3f4f6",
        foreground="#111827",
        font=("Segoe UI Semibold", 16),
        anchor="w",
    ).pack(fill="x")
    tk.Label(
        shell,
        text=(
            "Hold the selected key for local dictation. Double-tap it to start Ask Glean. "
            "Normal key combinations are ignored."
        ),
        background="#f3f4f6",
        foreground="#4b5563",
        font=("Segoe UI", 10),
        justify="left",
        wraplength=560,
        anchor="w",
        pady=8,
    ).pack(fill="x")

    selection = tk.StringVar(value=initial_choice.choice_id)
    descriptions = {choice.choice_id: choice.description for choice in trigger_choices()}
    description = tk.StringVar(value=initial_choice.description)

    choices_frame = tk.Frame(
        shell,
        background="#ffffff",
        highlightbackground="#d1d5db",
        highlightthickness=1,
        padx=14,
        pady=8,
    )
    choices_frame.pack(fill="x", pady=(8, 10))

    def update_description() -> None:
        description.set(descriptions[selection.get()])

    for choice in trigger_choices():
        ttk.Radiobutton(
            choices_frame,
            text=choice.display_name,
            value=choice.choice_id,
            variable=selection,
            command=update_description,
        ).pack(fill="x", pady=4)

    tk.Label(
        shell,
        textvariable=description,
        background="#f3f4f6",
        foreground="#374151",
        font=("Segoe UI", 9),
        justify="left",
        wraplength=560,
        anchor="w",
    ).pack(fill="x")

    fn_panel = tk.Frame(
        shell,
        background="#fffbeb",
        highlightbackground="#f59e0b",
        highlightthickness=1,
        padx=12,
        pady=10,
    )
    fn_panel.pack(fill="x", pady=(14, 16))
    tk.Label(
        fn_panel,
        text="Why isn't Fn selectable?",
        background="#fffbeb",
        foreground="#92400e",
        font=("Segoe UI Semibold", 9),
        anchor="w",
    ).pack(fill="x")
    tk.Label(
        fn_panel,
        text=(
            "Most laptop firmware handles Fn before Windows can see it. It can only be enabled "
            "later for laptop models that expose a reviewed Raw Input or HID signal."
        ),
        background="#fffbeb",
        foreground="#92400e",
        font=("Segoe UI", 9),
        justify="left",
        wraplength=530,
        anchor="w",
    ).pack(fill="x", pady=(3, 0))

    buttons = tk.Frame(shell, background="#f3f4f6")
    buttons.pack(fill="x")

    def accept() -> None:
        selected_choice[0] = selection.get()
        root.destroy()

    def cancel() -> None:
        root.destroy()

    ttk.Button(buttons, text="Cancel", command=cancel).pack(side="right")
    ttk.Button(buttons, text="Use this key", command=accept).pack(side="right", padx=(0, 8))
    root.protocol("WM_DELETE_WINDOW", cancel)
    root.bind("<Escape>", lambda _event: cancel())
    root.bind("<Return>", lambda _event: accept())
    root.update_idletasks()
    width = root.winfo_reqwidth()
    height = root.winfo_reqheight()
    x = max(0, (root.winfo_screenwidth() - width) // 2)
    y = max(0, (root.winfo_screenheight() - height) // 2)
    root.geometry(f"{width}x{height}+{x}+{y}")
    root.mainloop()
    return selected_choice[0]
