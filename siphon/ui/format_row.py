"""The Download page's Format row: every format's sound on a quality meter, and what it gets you, on the row and in
its list, so formats can be compared before one is chosen."""

from types import ModuleType

from gi.repository import Adw, GObject, Gtk


def spoken(core: ModuleType, fmt: str) -> str:
    """A format's meter and note in one sentence, for screen readers: "Excellent sound. Small files that…"."""
    return f"{core.FORMAT_QUALITY[fmt][1]} sound. {core.FORMAT_NOTES[fmt]}."


class QualityMeter(Gtk.Box):
    """How close a format keeps the site's own sound: blocks filled and coloured by it (style.css), and its word, so
    it reads without the colours."""

    def __init__(self, blocks: int) -> None:
        super().__init__(spacing=6, valign=Gtk.Align.CENTER)
        self.add_css_class("quality")
        self.bar = Gtk.LevelBar(mode=Gtk.LevelBarMode.DISCRETE, max_value=blocks, valign=Gtk.Align.CENTER)
        for offset in (Gtk.LEVEL_BAR_OFFSET_LOW, Gtk.LEVEL_BAR_OFFSET_HIGH, Gtk.LEVEL_BAR_OFFSET_FULL):
            self.bar.remove_offset_value(offset)  # coloured by the level itself (style.css), not GTK's thresholds
        self.word = Gtk.Label(css_classes=["caption"])
        self.append(self.bar)
        self.append(self.word)
        self._level = 0

    def show(self, level: int, word: str) -> None:
        self.remove_css_class(f"level-{self._level}")
        self._level = level
        self.add_css_class(f"level-{level}")
        self.bar.set_value(level)
        self.word.set_label(word)
        self.set_tooltip_text(f"{word} sound")  # on the row of a narrow window the word itself is hidden
        self.bar.update_property([Gtk.AccessibleProperty.LABEL, Gtk.AccessibleProperty.VALUE_TEXT],
                                 ["Sound quality", word])


class FormatView(Gtk.Box):
    """One format: its name, then its meter. In the list (for a list item) also its note under them, and the check
    mark a plain ComboRow's list puts on the chosen one."""

    def __init__(self, blocks: int, item: Gtk.ListItem | None = None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        listed = item is not None
        line = Gtk.Box(spacing=12)
        self.name = Gtk.Label(xalign=0, hexpand=listed)  # in the list the meters line up on the right
        self.meter = QualityMeter(blocks)
        self.check = Gtk.Image(icon_name="object-select-symbolic", visible=listed, opacity=0)
        line.append(self.name)
        line.append(self.meter)
        line.append(self.check)
        self.append(line)
        # 30 wide: every note in two lines, and a list about as narrow as the window's narrowest row
        self.note = Gtk.Label(xalign=0, wrap=True, max_width_chars=30, visible=listed,
                              css_classes=["caption", "dim-label"])
        self.append(self.note)
        if listed:
            item.connect("notify::selected", lambda item, _pspec: self.check.set_opacity(item.get_selected()))

    def show(self, core: ModuleType, fmt: str) -> None:
        self.name.set_label(core.FORMAT_LABELS[fmt])
        self.meter.show(*core.FORMAT_QUALITY[fmt])
        self.note.set_label(core.FORMAT_NOTES[fmt])


class FormatRow(Adw.ComboRow):
    """The chosen format and its meter on the row, its note as the subtitle; the list shows each format's meter and
    note under its name."""

    # A narrow window: the row's meter without its word, so the note beside it keeps some width (360 px: three
    # lines, not four). The blocks, the tooltip, the list and screen readers still give the word.
    narrow = GObject.Property(type=bool, default=False)

    def __init__(self, core: ModuleType, fmt: str) -> None:
        self.core = core
        self._formats = {core.FORMAT_LABELS[f]: f for f in core.FORMATS}  # the model's strings are the labels
        super().__init__(title="Format", model=Gtk.StringList.new(list(self._formats)),
                         factory=self._factory(listed=False), list_factory=self._factory(listed=True))
        self.add_css_class("format")  # added: css_classes=[...] would drop the row's own "combo"
        self.set_selected(core.FORMATS.index(fmt))
        self.connect("notify::selected", lambda _row, _pspec: self._describe())
        self._describe()

    @property
    def format(self) -> str:
        return self.core.FORMATS[self.get_selected()]

    def _describe(self) -> None:
        self.set_subtitle(self.core.FORMAT_NOTES[self.format])
        # After the subtitle, which the row also gives screen readers as its description: the meter's word first.
        self.update_property([Gtk.AccessibleProperty.DESCRIPTION], [spoken(self.core, self.format)])

    def _factory(self, listed: bool) -> Gtk.SignalListItemFactory:
        """The row's (listed=False) or the list's view of a format."""
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._setup_listed if listed else self._setup_chosen)
        factory.connect("bind", self._bind)
        return factory

    def _setup_listed(self, _factory: Gtk.SignalListItemFactory, item: Gtk.ListItem) -> None:
        item.set_child(FormatView(self.core.QUALITY_BLOCKS, item))

    def _setup_chosen(self, _factory: Gtk.SignalListItemFactory, item: Gtk.ListItem) -> None:
        view = FormatView(self.core.QUALITY_BLOCKS)
        # a binding, not a list of views: the row sets up a new one for every choice (measured: 50 in 50 choices)
        self.bind_property("narrow", view.meter.word, "visible",
                           GObject.BindingFlags.SYNC_CREATE | GObject.BindingFlags.INVERT_BOOLEAN)
        item.set_child(view)

    def _bind(self, _factory: Gtk.SignalListItemFactory, item: Gtk.ListItem) -> None:
        label = item.get_item().get_string()
        fmt = self._formats[label]
        view = item.get_child()
        view.show(self.core, fmt)
        view.check.set_opacity(item.get_selected())
        item.set_accessible_label(label)
        item.set_accessible_description(spoken(self.core, fmt))
