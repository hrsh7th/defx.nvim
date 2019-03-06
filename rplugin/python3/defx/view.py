# ============================================================================
# FILE: view.py
# AUTHOR: Shougo Matsushita <Shougo.Matsu at gmail.com>
# License: MIT license
# ============================================================================

import time
import typing
from pathlib import Path

from defx.base.column import Base as Column
from defx.clipboard import Clipboard
from defx.context import Context
from defx.defx import Defx
from defx.util import error, import_plugin, safe_call, Nvim


class View(object):

    def __init__(self, vim: Nvim, index: int) -> None:
        self._vim: Nvim = vim
        self._candidates: typing.List[typing.Dict[str, typing.Any]] = []
        self._selected_candidates: typing.List[str] = []
        self._opened_candidates: typing.List[str] = []
        self._clipboard = Clipboard()
        self._bufnr = -1
        self._winid = -1
        self._index = index
        self._bufname = '[defx]'
        self._buffer: Nvim.buffer = None
        self._prev_action = ''
        self._prev_highlight_commands: typing.List[str] = []

    def init(self, paths: typing.List[str],
             context: typing.Dict[str, typing.Any],
             clipboard: Clipboard) -> None:
        context['wincol'] = int(context['wincol'])
        context['winheight'] = int(context['winheight'])
        context['winrow'] = int(context['winrow'])
        context['winwidth'] = int(context['winwidth'])
        context['prev_bufnr'] = int(context['prev_bufnr'])
        context['prev_winid'] = int(context['prev_winid'])
        self._context = Context(**context)
        self._bufname = f'[defx] {self._context.buffer_name}-{self._index}'

        if not self.init_buffer(paths):
            self._winid = self._vim.call('win_getid')
            return

        self._candidates = []
        self._selected_candidates = []
        self._opened_candidates = []
        self._context = Context(**context)
        self._clipboard = clipboard

        # Initialize defx
        self._defxs: typing.List[Defx] = []
        self._buffer.vars['defx']['paths'] = paths
        for [index, path] in enumerate(paths):
            self._defxs.append(Defx(self._vim, self._context, path, index))

        self.init_columns(self._context.columns.split(':'))

        self.redraw(True)

        for defx in self._defxs:
            if self._context.search:
                self.search_tree(self._context.search, defx._index)
            else:
                self.init_cursor(defx)

    def init_columns(self, columns: typing.List[str]) -> None:
        # Initialize columns
        self._columns: typing.List[Column] = []
        self._all_columns: typing.Dict[str, Column] = {}

        for path_column in self.load_custom_columns():
            column = import_plugin(path_column, 'column', 'Column')
            if not column:
                continue

            column = column(self._vim)
            if column.name not in self._all_columns:
                self._all_columns[column.name] = column

        custom = self._vim.call('defx#custom#_get')['column']
        self._columns = [self._all_columns[x]
                         for x in columns if x in self._all_columns]
        for column in self._columns:
            if column.name in custom:
                column.vars.update(custom[column.name])
            column.on_init(self._context)
            column.syntax_name = 'Defx_' + column.name

    def init_buffer(self, paths: typing.List[str]) -> bool:
        if self._context.split == 'tab':
            self._vim.command('tabnew')

        winnr = self._vim.call('bufwinnr', self._bufname)
        if winnr > 0:
            self._vim.command(f'{winnr}wincmd w')
            if self._context.toggle:
                self.quit()
                return False
            return True

        if (self._vim.current.buffer.options['modified'] and
                not self._vim.options['hidden'] and
                self._context.split == 'no'):
            self._context = self._context._replace(split='vertical')

        if (self._context.split == 'floating'
                and self._vim.call('exists', '*nvim_open_win')):
            # Use floating window
            self._vim.call(
                'nvim_open_win',
                self._vim.call('bufnr', '%'), True,
                self._context.winwidth,
                self._context.winheight, {
                    'relative': 'editor',
                    'row': self._context.winrow,
                    'col': self._context.wincol,
                })

        # Create new buffer
        vertical = 'vertical' if self._context.split == 'vertical' else ''
        no_split = self._context.split in ['no', 'tab', 'floating']
        if self._vim.call('bufexists', self._bufnr):
            command = ('buffer' if no_split else 'sbuffer')
            self._vim.command(
                'silent keepalt %s %s %s %s' % (
                    self._context.direction,
                    vertical,
                    command,
                    self._bufnr,
                )
            )
            if self._context.resume:
                return False
        else:
            command = ('edit' if no_split else 'new')
            self._vim.call(
                'defx#util#execute_path',
                'silent keepalt %s %s %s ' % (
                    self._context.direction,
                    vertical,
                    command,
                ),
                self._bufname)

        self._buffer = self._vim.current.buffer
        self._bufnr = self._buffer.number
        self._winid = self._vim.call('win_getid')

        window_options = self._vim.current.window.options
        window_options['list'] = False
        window_options['wrap'] = False

        if (self._context.split == 'vertical'
                and self._context.winwidth > 0):
            window_options['winfixwidth'] = True
            self._vim.command(f'vertical resize {self._context.winwidth}')
        elif (self._context.split == 'horizontal' and
              self._context.winheight > 0):
            window_options['winfixheight'] = True
            self._vim.command(f'resize {self._context.winheight}')

        buffer_options = self._buffer.options
        buffer_options['buftype'] = 'nofile'
        buffer_options['swapfile'] = False
        buffer_options['modeline'] = False
        buffer_options['filetype'] = 'defx'
        buffer_options['modifiable'] = False
        buffer_options['modified'] = False

        self._buffer.vars['defx'] = {
            'context': self._context._asdict(),
            'paths': paths,
        }

        if not self._context.listed:
            buffer_options['buflisted'] = False

        self.execute_commands([
            'silent doautocmd FileType defx',
            'autocmd! defx * <buffer>',
        ])
        self._vim.command('autocmd defx '
                          'CursorHold,WinEnter,FocusGained <buffer> '
                          'call defx#call_async_action("check_redraw")')

        self._prev_highlight_commands = []

        return True

    def init_length(self) -> None:
        start = 1
        for column in self._columns:
            column.start = start
            length = column.length(
                self._context._replace(targets=self._candidates))
            column.end = start + length
            start += length + 1

    def update_syntax(self) -> None:
        commands: typing.List[str] = []
        for column in self._columns:
            commands.append(
                'silent! syntax clear ' + column.syntax_name)
            for syntax in column.syntaxes():
                commands.append(
                    'silent! syntax clear ' + syntax)
            commands.append(
                'syntax region ' + column.syntax_name +
                r' start=/\%' + str(column.start) + r'v/ end=/\%' +
                str(column.end) + 'v/ keepend oneline')
            commands += column.highlight_commands()

        if commands == self._prev_highlight_commands:
            # Skip highlights
            return

        self._prev_highlight_commands = commands

        self.execute_commands(commands)

    def debug(self, expr: typing.Any) -> None:
        error(self._vim, expr)

    def print_msg(self, expr: typing.Any) -> None:
        self._vim.call('defx#util#print_message', expr)

    def execute_commands(self, commands: typing.List[str]) -> None:
        self._vim.command(' | '.join(commands))

    def quit(self) -> None:
        winnr = self._vim.call('bufwinnr', self._bufname)
        if winnr < 0:
            return

        if winnr != self._vim.call('winnr'):
            # Use current window
            self._context = self._context._replace(
                prev_winid=self._vim.call('win_getid'))

        self._vim.command(f'{winnr}wincmd w')

        if self._context.split in ['no', 'tab']:
            if self._vim.call('bufexists', self._context.prev_bufnr):
                self._vim.command('buffer ' + str(self._context.prev_bufnr))
            else:
                self._vim.command('enew')
        else:
            if self._vim.call('winnr', '$') != 1:
                self._vim.command('close')
                self._vim.call('win_gotoid', self._context.prev_winid)
            else:
                self._vim.command('enew')

    def init_candidates(self) -> None:
        self._candidates = []
        for defx in self._defxs:
            root = defx.get_root_candidate()
            root['_defx_index'] = defx._index
            root['level'] = 0
            defx._mtime = root['action__path'].stat().st_mtime

            self._candidates = [root]
            self._init_candidates(defx, root['action__path'], 0)

        self._selected_candidates = [candidate['action__path'] for candidate in self._candidates if candidate['action__path'] in self._selected_candidates]
        self._opened_candidates = [candidate['action__path'] for candidate in self._candidates if candidate['action__path'] in self._opened_candidates]

    def _init_candidates(self, defx, path, level) -> None:
        candidates = defx.gather_candidates(path)
        for candidate in candidates:
            candidate['_defx_index'] = defx._index
            candidate['level'] = level
            self._candidates.append(candidate)
            if candidate['action__path'] in self._opened_candidates:
                self._init_candidates(defx, candidate['action__path'], level + 1)

    def redraw(self, is_force: bool = False) -> None:
        """
        Redraw defx buffer.
        """

        if self._buffer != self._vim.current.buffer:
            return

        start = time.time()

        prev_linenr = self._vim.call('line', '.')
        prev = self.get_cursor_candidate(prev_linenr)

        if is_force:
            self.init_candidates()
            self.init_length()
            self.update_syntax()

        # Set flags
        for candidate in self._candidates:
            candidate['is_selected'] = False
            candidate['is_opened'] = False
        for path in self._selected_candidates:
            for candidate in self._candidates:
                if candidate['action__path'] is path:
                    candidate['is_selected'] = True
        for path in self._opened_candidates:
            for candidate in self._candidates:
                if candidate['action__path'] is path:
                    candidate['is_opened'] = True

        for column in self._columns:
            column.on_redraw(self._context)

        if self._buffer != self._vim.current.buffer:
            return

        self._buffer.options['modifiable'] = True
        self._buffer[:] = [
            self.get_columns_text(self._context, x)
            for x in self._candidates
        ]
        self._buffer.options['modifiable'] = False
        self._buffer.options['modified'] = False

        self._vim.call('cursor', [prev_linenr, 0])

        if prev:
            self.search_file(prev['action__path'], prev['_defx_index'])

        if self._context.profile:
            error(self._vim, f'redraw time = {time.time() - start}')

    def get_columns_text(self, context: Context,
                         candidate: typing.Dict[str, typing.Any]) -> str:
        text = ''
        for column in self._columns:
            if text:
                text += ' '
            text += column.get(context, candidate)
        return text

    def get_cursor_candidate(
            self, cursor: int) -> typing.Dict[str, typing.Any]:
        if len(self._candidates) < cursor:
            return {}
        else:
            return self._candidates[cursor - 1]

    def get_selected_candidates(
            self, cursor: int, index: int
    ) -> typing.List[typing.Dict[str, typing.Any]]:
        if not self._candidates:
            return []
        if not self._selected_candidates:
            candidates = [self.get_cursor_candidate(cursor)]
        else:
            candidates = [candidate for candidate in self._candidates if candidate['action__path'] in self._selected_candidates]
        return [x for x in candidates
                if x.get('_defx_index', -1) == index]

    def cd(self, defx: Defx, path: str, cursor: int) -> None:
        # Save previous cursor position
        history = defx._cursor_history
        history[defx._cwd] = self.get_cursor_candidate(cursor)['action__path']

        global_histories = self._vim.vars['defx#_histories']
        global_histories.append(defx._cwd)
        self._vim.vars['defx#_histories'] = global_histories

        defx.cd(path)
        self.redraw(True)
        self.init_cursor(defx)
        if path in history:
            self.search_file(history[path], defx._index)
        self._selected_candidates = []
        self._opened_candidates = []

        self.update_paths(defx._index, path)

    def update_paths(self, index: int, path: str) -> None:
        var_defx = self._buffer.vars['defx']
        var_defx['paths'][index] = path
        self._buffer.vars['defx'] = var_defx

    def search_tree(self, start: str, index: int) -> bool:
        path = Path(start)
        while True:
            if self.search_file(path, index):
                return True
            if path.parent == path:
                break
            path = path.parent
        return False

    def get_candidate_pos(self, path: Path, index: int) -> int:
        for [pos, candidate] in enumerate(self._candidates):
            if (candidate['_defx_index'] == index and
                    candidate['action__path'] == path):
                return pos
        return -1

    def search_file(self, path: Path, index: int) -> bool:
        linenr = 1
        target = str(path)
        if target and target[-1] == '/':
            target = target[:-1]
        for candidate in self._candidates:
            if (candidate['_defx_index'] == index and
                    str(candidate['action__path']) == target):
                self._vim.call('cursor', [linenr, 1])
                return True
            linenr += 1
        return False

    def init_cursor(self, defx: Defx) -> None:
        self.search_file(Path(defx._cwd), defx._index)

        # Move to next
        self._vim.call('cursor', [self._vim.call('line', '.') + 1, 1])

    def do_action(self, action_name: str,
                  action_args: typing.List[str],
                  new_context: typing.Dict[str, typing.Any]) -> None:
        """
        Do "action" action.
        """
        cursor = new_context['cursor']

        defx_targets = {
            x._index: self.get_selected_candidates(cursor, x._index)
            for x in self._defxs}
        all_targets: typing.List[typing.Dict[str, typing.Any]] = []
        for targets in defx_targets.values():
            all_targets += targets

        import defx.action as action
        for defx in [x for x in self._defxs
                     if not all_targets or defx_targets[x._index]]:
            context = self._context._replace(
                targets=defx_targets[defx._index],
                args=action_args,
                cursor=cursor
            )
            ret = action.do_action(self, defx, action_name, context)
            if ret:
                error(self._vim, 'Invalid action_name:' + action_name)
                return

    def load_custom_columns(self) -> typing.List[Path]:
        rtp_list = self._vim.options['runtimepath'].split(',')
        result: typing.List[Path] = []

        for path in rtp_list:
            column_path = Path(path).joinpath(
                'rplugin', 'python3', 'defx', 'column')
            if safe_call(column_path.is_dir):
                result += column_path.glob('*.py')

        return result
