import adsk.core, adsk.fusion, adsk.cam, traceback
import re
import os, itertools, json

_app = None
_ui = None
_handlers = []

CMD_ID   = 'batchParametricExportCmd'
CMD_NAME = 'Batch parametric export'
CMD_DESC = 'Select bodies and components to export and process them using all parameter combinations.'

_param_registry = {}  # key: chk_id -> {'param': adsk.fusion.UserParameter, 'text_id': str}

_SIMPLE_LITERAL_RE = re.compile(r'^\s*([-+]?\d+(?:\.\d+)?)\s*([A-Za-z%°/]+)?\s*$')
_RANGE_RE = re.compile(r'^\s*([-+]?\d+)\s*\.\.\s*([-+]?\d+)\s*$')

# --- filename template wiring ---
FORMAT_ID = 'outFormat'
FILENAME_ID = 'filenameTemplate'
_current_inputs = None  # set during command; used by change handler

# keep parameter order for template construction
_param_registry = {}   # chk_id -> {'param': UserParameter, 'text_id': str, 'name': str, 'order': int}
_param_order = []      # list of parameter names in UI order

_EXT_MAP = {'STEP': 'step', 'STL': 'stl', '3MF': '3mf', 'OBJ': 'obj'}

OUTPUT_DIR_ID = 'outputDir'
OUTPUT_BROWSE_ID = 'outputBrowse'
_last_folder = ''  # remember the last chosen folder during the session

SETTINGS_GROUP = 'BatchParametricExport'
SETTINGS_NAME  = 'state_v1'
_settings = {}

ADDIN_DIR       = os.path.dirname(os.path.abspath(__file__))
RESOURCE_FOLDER = os.path.join(ADDIN_DIR, 'resources')
WORKSPACE_ID    = 'FusionSolidEnvironment'
PANEL_ID        = 'UtilityPanel'
TARGET_TAB_ID   = ''

# name -> ('body'|'component', ref)
_item_registry = {}

def _load_settings(design):
    if not design:
        return {}
    a = design.attributes.itemByName(SETTINGS_GROUP, SETTINGS_NAME)
    if not a or not a.value:
        return {}
    try:
        return json.loads(a.value)
    except Exception:
        return {}

def _save_settings(design, state: dict):
    if not design:
        return
    try:
        design.attributes.add(SETTINGS_GROUP, SETTINGS_NAME, json.dumps(state))
    except Exception:
        pass

def _remove_ui():
    if not _ui:
        return
    ws = _ui.workspaces.itemById(WORKSPACE_ID)
    if ws:
        panel = ws.toolbarPanels.itemById(PANEL_ID)
        if panel:
            ctrl = panel.controls.itemById(CMD_ID)
            if ctrl:
                ctrl.deleteMe()
    cmd_def = _ui.commandDefinitions.itemById(CMD_ID)
    if cmd_def:
        cmd_def.deleteMe()

def _collect_state(inputs, objects, sel_param_names, param_values, fmt_name, template, out_dir) -> dict:
    sel_bodies = []
    sel_comps = []
    for chk_id, (kind, ref) in _item_registry.items():
        chk = adsk.core.BoolValueCommandInput.cast(inputs.itemById(chk_id))
        if chk and chk.value:
            try:
                tok = ref.entityToken
            except Exception:
                continue
            if kind == 'body':
                sel_bodies.append(tok)
            else:
                sel_comps.append(tok)

    params_state = {}
    for chk_id, meta in _param_registry.items():
        chk = adsk.core.BoolValueCommandInput.cast(inputs.itemById(chk_id))
        txt = adsk.core.StringValueCommandInput.cast(inputs.itemById(meta['text_id']))
        params_state[meta['name']] = {
            'checked': bool(chk.value) if chk else False,
            'values': (txt.value or '') if txt else '',
        }

    return {
        'selBodies': sel_bodies,
        'selComponents': sel_comps,
        'params': params_state,
        'format': fmt_name,
        'template': template,
        'outDir': out_dir,
    }

def run(context):
    try:
        global _app, _ui
        _app = adsk.core.Application.get()
        _ui  = _app.userInterface

        _remove_ui()

        icon_path = RESOURCE_FOLDER if os.path.isdir(RESOURCE_FOLDER) else ''
        cmd_def = _ui.commandDefinitions.addButtonDefinition(CMD_ID, CMD_NAME, CMD_DESC, icon_path)

        class CommandCreatedHandler(adsk.core.CommandCreatedEventHandler):
            def notify(self, args: adsk.core.CommandCreatedEventArgs):
                try:
                    cmd = args.command
                    cmd.setDialogMinimumSize(560, 480)
                    cmd.setDialogInitialSize(560, 600)
                    cmd.okButtonText = 'Export'
                    inputs = cmd.commandInputs

                    global _settings
                    _settings = _load_settings(adsk.fusion.Design.cast(_app.activeProduct))

                    sel_group = inputs.addGroupCommandInput('grpSelection', 'Selection')
                    sel_group.isExpanded = True
                    _createObjectsTable(sel_group.children)

                    param_group = inputs.addGroupCommandInput('grpParams', 'Parameters')
                    param_group.isExpanded = True
                    _createParametersTable(param_group.children)

                    out_group = inputs.addGroupCommandInput('grpOutput', 'Output')
                    out_group.isExpanded = True
                    og = out_group.children

                    out_tbl = og.addTableCommandInput('outTable', '', 3, '2:8:2')
                    out_tbl.columnSpacing = 6
                    out_tbl.rowSpacing = 4
                    out_tbl.minimumVisibleRows = 3
                    out_tbl.maximumVisibleRows = 3

                    lbl_fmt = og.addTextBoxCommandInput('lblFormat', '', 'Format', 1, True)
                    fmt = og.addDropDownCommandInput(FORMAT_ID, '',
                                                    adsk.core.DropDownStyles.TextListDropDownStyle)
                    saved_fmt = _settings.get('format', 'OBJ')
                    for opt in ('STEP', 'STL', '3MF', 'OBJ'):
                        fmt.listItems.add(opt, opt == saved_fmt, '')
                    out_tbl.addCommandInput(lbl_fmt, 0, 0)
                    out_tbl.addCommandInput(fmt, 0, 1)

                    lbl_tmpl = og.addTextBoxCommandInput('lblFilename', '', 'Filename', 1, True)
                    tmpl = og.addStringValueInput(FILENAME_ID, '', '')
                    tmpl.tooltip = 'Uses {name} and selected parameter names. Example: {name}_{width}_{height}.obj'
                    out_tbl.addCommandInput(lbl_tmpl, 1, 0)
                    out_tbl.addCommandInput(tmpl, 1, 1)

                    lbl_dir = og.addTextBoxCommandInput('lblFolder', '', 'Folder', 1, True)
                    initial_dir = _settings.get('outDir', '') or _last_folder or ''
                    path_in = og.addStringValueInput(OUTPUT_DIR_ID, '', initial_dir)
                    browseBtn = og.addBoolValueInput(OUTPUT_BROWSE_ID, 'Browse…', False, '', False)
                    out_tbl.addCommandInput(lbl_dir, 2, 0)
                    out_tbl.addCommandInput(path_in, 2, 1)
                    out_tbl.addCommandInput(browseBtn, 2, 2)

                    change_handler = InputChangedHandler()
                    cmd.inputChanged.add(change_handler)
                    _handlers.append(change_handler)

                    global _current_inputs
                    _current_inputs = inputs

                    _updateFilenameTemplate()
                    saved_template = _settings.get('template', '')
                    if saved_template:
                        tmpl.value = saved_template

                    # Events
                    exec_handler = ExecuteHandler()
                    destroy_handler = CommandDestroyedHandler()
                    cmd.execute.add(exec_handler)
                    cmd.destroy.add(destroy_handler)
                    _handlers.extend([exec_handler, destroy_handler])

                    validate_handler = ValidateHandler()
                    cmd.validateInputs.add(validate_handler)
                    _handlers.append(validate_handler)

                except:
                    _ui.messageBox('Command create failed:\n{}'.format(traceback.format_exc()))

        class ValidateHandler(adsk.core.ValidateInputsEventHandler):
            def notify(self, args: adsk.core.ValidateInputsEventArgs):
                try:
                    ok, msg = _validate_all(args.inputs)
                    args.areInputsValid = ok
                    # Optional: show inline error text (no popups)
                    err = adsk.core.TextBoxCommandInput.cast(args.inputs.itemById('inline_error'))
                    if not err:
                        # create it once (read-only, full width)
                        err = args.inputs.addTextBoxCommandInput('inline_error', '', '', 2, True)
                        err.isFullWidth = True
                    err.text = '' if ok else f'{msg}'
                    err.isVisible = not ok
                except:
                    # if validation itself fails, keep OK disabled
                    args.areInputsValid = False

        class ExecuteHandler(adsk.core.CommandEventHandler):
            def notify(self, args: adsk.core.CommandEventArgs):
                try:
                    design = adsk.fusion.Design.cast(_app.activeProduct)
                    if not design:
                        _ui.messageBox('No active design.')
                        return

                    inputs = args.command.commandInputs

                    objects = _get_selected_objects(inputs)                     # [(kind, ref, name), ...]
                    sel_param_names, param_values, is_text_map = _get_selected_params_and_values(inputs)
                    fmt = adsk.core.DropDownCommandInput.cast(inputs.itemById(FORMAT_ID))
                    fmt_name = fmt.selectedItem.name if (fmt and fmt.selectedItem) else 'OBJ'
                    ext = _EXT_MAP.get(fmt_name, 'obj')
                    template = adsk.core.StringValueCommandInput.cast(inputs.itemById(FILENAME_ID)).value.strip()
                    out_dir = adsk.core.StringValueCommandInput.cast(inputs.itemById(OUTPUT_DIR_ID)).value.strip()

                    _save_settings(design, _collect_state(inputs, objects, sel_param_names, param_values, fmt_name, template, out_dir))

                    # cache originals to restore
                    orig_param_expr = {}
                    orig_body_vis, orig_occ_vis = _snapshot_visibility(design)

                    # materialize all combos once (preserves UI order)
                    ordered_lists = [param_values[p] for p in sel_param_names]
                    all_combos = list(itertools.product(*ordered_lists))

                    total = len(all_combos) * len(objects)
                    prog = _progress_start('Batch export', total)
                    step = 0

                    try:
                        for combo_idx, combo in enumerate(all_combos, start=1):
                            # 1) set params for this combo and recompute once
                            _set_user_params(design, combo, sel_param_names, is_text_map, orig_param_expr)
                            _compute(design)

                            # reusable mapping for filenames
                            pv_map = dict(zip(sel_param_names, combo))

                            # 2) export each selected object under this param state
                            for obj_idx, (kind, ref, obj_name) in enumerate(objects, start=1):
                                # build filename once for progress + export
                                out_filename = _build_filename(template, obj_name, pv_map)
                                fullpath = os.path.join(out_dir, out_filename)

                                step += 1
                                combo_note = ', '.join(f'{p}={pv_map[p]}' for p in sel_param_names)
                                _progress_update(prog, step, note=f'[{combo_idx}/{len(all_combos)}] {combo_note} -> {out_filename}')
                                if prog.wasCancelled:
                                    raise KeyboardInterrupt('User cancelled')

                                if fmt_name.upper() == 'STEP':
                                    # isolate just this object for STEP
                                    _isolate_for_step(design, kind, ref)
                                    # No need to recompute geometry; visibility changes don’t require it
                                    ok = _export_step(design, fullpath)
                                    # restore vis right after exporting this object to keep scene sane
                                    _restore_visibility(design, orig_body_vis, orig_occ_vis)
                                else:
                                    # mesh formats target the entity directly
                                    ok = _export_mesh(design, ref, fullpath, fmt_name)

                                if not ok:
                                    raise RuntimeError(f'Export failed: {fullpath}')

                        _ui.messageBox('Success: exports complete.')

                    except KeyboardInterrupt:
                        _ui.messageBox('Export cancelled.')
                    finally:
                        _progress_end(prog)
                        _restore_user_params(design, orig_param_expr)
                        _restore_visibility(design, orig_body_vis, orig_occ_vis)
                        _compute(design)

                except Exception as ex:
                    _ui.messageBox('Execute failed:\n{}'.format(traceback.format_exc()))

        class CommandDestroyedHandler(adsk.core.CommandEventHandler):
            def notify(self, args):
                _item_registry.clear()
                _param_registry.clear()
                _param_order.clear()
                _settings.clear()
                global _current_inputs
                _current_inputs = None

        class InputChangedHandler(adsk.core.InputChangedEventHandler):
            def notify(self, args: adsk.core.InputChangedEventArgs):
                global _last_folder
                try:
                    inp = args.input
                    if not inp:
                        return

                    # Rebuild filename template when format or param checkboxes change
                    if inp.id == FORMAT_ID or inp.id.startswith('chk_param_'):
                        _updateFilenameTemplate()
                        return

                    # Browse for folder
                    if inp.id == OUTPUT_BROWSE_ID and inp.value:  # clicked
                        dlg = _ui.createFolderDialog()
                        dlg.title = 'Select output folder'
                        if _last_folder:
                            dlg.initialDirectory = _last_folder
                        res = dlg.showDialog()
                        if res == adsk.core.DialogResults.DialogOK:
                            folder = dlg.folder
                            sv = adsk.core.StringValueCommandInput.cast(_current_inputs.itemById(OUTPUT_DIR_ID))
                            if sv:
                                sv.value = folder
                            _last_folder = folder
                        # reset so button can be clicked again
                        inp.value = False

                except:
                    _ui.messageBox('inputChanged failed:\n{}'.format(traceback.format_exc()))


        created_handler = CommandCreatedHandler()
        cmd_def.commandCreated.add(created_handler)
        _handlers.append(created_handler)

        ws = _ui.workspaces.itemById(WORKSPACE_ID)
        if ws:
            panel = ws.toolbarPanels.itemById(PANEL_ID)
            if panel:
                ctrl = panel.controls.addCommand(cmd_def)
                ctrl.isPromoted = True
                ctrl.isPromotedByDefault = True

    except:
        if _ui:
            _ui.messageBox('Add-in run failed:\n{}'.format(traceback.format_exc()))


def _updateFilenameTemplate():
    if _current_inputs is None:
        return

    # collect checked params in UI order, using your explicit placeholder form
    selected = []
    for pname in _param_order:
        # find the checkbox for this pname
        for chk_id, meta in _param_registry.items():
            if meta['name'] == pname:
                chk = adsk.core.BoolValueCommandInput.cast(_current_inputs.itemById(chk_id))
                if chk and chk.value:
                    selected.append(f'{{{pname}}}')  # your change
                break

    # resolve extension safely
    fmt = adsk.core.DropDownCommandInput.cast(_current_inputs.itemById(FORMAT_ID))
    ext_name = 'OBJ'
    if fmt and fmt.selectedItem:
        ext_name = fmt.selectedItem.name
    ext = _EXT_MAP.get(ext_name, 'obj')

    middle = ('_' + '_'.join(selected)) if selected else ''
    template = f'{{name}}{middle}.{ext}'

    sv = adsk.core.StringValueCommandInput.cast(_current_inputs.itemById(FILENAME_ID))
    if sv:
        sv.value = template

def stop(context):
    try:
        _remove_ui()
        _handlers.clear()
        _item_registry.clear()
        _param_registry.clear()
        _param_order.clear()
        _settings.clear()
    except:
        if _ui:
            _ui.messageBox('Add-in stop failed:\n{}'.format(traceback.format_exc()))

def _createObjectsTable(inputs: adsk.core.CommandInputs):
    table = inputs.addTableCommandInput('itemsTable', '', 2, '1:8')
    table.columnSpacing = 6
    table.rowSpacing = 2
    table.minimumVisibleRows = 1
    table.maximumVisibleRows = 8

    design = adsk.fusion.Design.cast(_app.activeProduct)
    if not design:
        return
    root = design.rootComponent
    occs = root.occurrences

    saved_bodies = set(_settings.get('selBodies', []))
    saved_comps  = set(_settings.get('selComponents', []))
    has_saved_selection = bool(saved_bodies or saved_comps)

    row = 0

    for i in range(root.bRepBodies.count):
        b = root.bRepBodies.item(i)
        chk_id = f'chk_body_{i}'
        initial = (b.entityToken in saved_bodies) if has_saved_selection else True
        chk = inputs.addBoolValueInput(chk_id, '', True, '', initial)
        lbl = inputs.addTextBoxCommandInput(f'lbl_body_{i}', '', f'[Body] {b.name}', 1, True)
        table.addCommandInput(chk, row, 0)
        table.addCommandInput(lbl, row, 1)
        _item_registry[chk_id] = ('body', b)
        row += 1

    for j in range(occs.count):
        occ = occs.item(j)
        if occ.component == root:
            continue
        comp = occ.component
        chk_id = f'chk_comp_{j}'
        initial = (occ.entityToken in saved_comps) if has_saved_selection else True
        chk = inputs.addBoolValueInput(chk_id, '', True, '', initial)
        lbl = inputs.addTextBoxCommandInput(f'lbl_comp_{j}', '', f'[Comp] {comp.name}', 1, True)
        table.addCommandInput(chk, row, 0)
        table.addCommandInput(lbl, row, 1)
        _item_registry[chk_id] = ('component', occ)
        row += 1

def _is_simple_literal(expr: str) -> bool:
    return bool(expr and _SIMPLE_LITERAL_RE.match(expr))

def _is_text_param(p) -> bool:
    try:
        return p.valueType == 1
    except:
        return getattr(p, 'unit', '') == 'Text'

def _text_param_value(p) -> str:
    try:
        return p.textValue
    except:
        expr = p.expression or ''
        return expr.strip().strip("'").strip('"')

def _expand_range(tok: str):
    m = _RANGE_RE.match(tok)
    if not m:
        return None
    a_str, b_str = m.group(1), m.group(2)
    a, b = int(a_str), int(b_str)
    a_abs = a_str.lstrip('+-')
    b_abs = b_str.lstrip('+-')
    width = max(len(a_abs), len(b_abs))
    pad = width > 1 and (a_abs.startswith('0') or b_abs.startswith('0'))
    step = 1 if b >= a else -1
    out = []
    for n in range(a, b + step, step):
        body = f'{abs(n):0{width}d}' if pad else f'{abs(n)}'
        out.append(('-' if n < 0 else '') + body)
    return out

def _format_expr_2dec(expr: str) -> str:
    """
    Format a parameter expression like '12 mm' into '12.00 mm'.
    If no unit, returns '12.00'. If it isn't a simple literal, just return expr.
    """
    m = _SIMPLE_LITERAL_RE.match(expr or '')
    if not m:
        return expr or ''
    num = float(m.group(1))
    unit = (m.group(2) or '').strip()
    return f'{num:.2f}{" " + unit if unit else ""}'

def _safe_id(text: str) -> str:
    return ''.join(c if c.isalnum() else '_' for c in (text or ''))

def _createParametersTable(inputs: adsk.core.CommandInputs):
    """4 columns: [✔][Name][Current Value][Values to iterate]; non-formula user params only."""
    design = adsk.fusion.Design.cast(_app.activeProduct)
    if not design:
        return

    hint = inputs.addTextBoxCommandInput(
        'params_hint', '',
        'Semicolon-separated values (1; 2.5; 3) or range (001..099)', 1, True
    )
    hint.isFullWidth = True

    tbl = inputs.addTableCommandInput('paramsTable', '', 4, '1:4:3:8')
    tbl.columnSpacing = 6
    tbl.rowSpacing = 2
    tbl.minimumVisibleRows = 2
    tbl.maximumVisibleRows = 10

    hdr_name = inputs.addTextBoxCommandInput('hdr_param_name', '', '<b>Parameter</b>', 1, True)
    hdr_cur  = inputs.addTextBoxCommandInput('hdr_param_cur',  '', '<b>Current</b>', 1, True)
    hdr_val  = inputs.addTextBoxCommandInput('hdr_param_val',  '', '<b>Values to iterate</b>', 1, True)
    tbl.addCommandInput(hdr_name, 0, 1)
    tbl.addCommandInput(hdr_cur,  0, 2)
    tbl.addCommandInput(hdr_val,  0, 3)

    ups = design.userParameters
    if not ups or ups.count == 0:
        info = inputs.addTextBoxCommandInput('no_params', '', 'No user parameters found.', 1, True)
        tbl.addCommandInput(info, 1, 0)
        return

    _param_order.clear()

    saved_params = _settings.get('params', {})
    has_saved_state = bool(_settings)

    row = 1
    for i in range(ups.count):
        p = ups.item(i)
        is_text = _is_text_param(p)
        if not is_text and not _is_simple_literal(p.expression):
            continue

        base = _safe_id(p.name)
        chk_id  = f'chk_param_{base}'
        name_id = f'lbl_param_{base}'
        val_id  = f'lbl_value_{base}'
        txt_id  = f'txt_values_{base}'

        saved = saved_params.get(p.name)
        if saved is not None:
            chk_initial = bool(saved.get('checked', False))
            txt_initial = saved.get('values', '') or ''
        else:
            chk_initial = not has_saved_state
            txt_initial = ''

        chk = inputs.addBoolValueInput(chk_id, '', True, '', chk_initial)
        tbl.addCommandInput(chk, row, 0)

        name_lbl = inputs.addTextBoxCommandInput(name_id, '', p.name, 1, True)
        tbl.addCommandInput(name_lbl, row, 1)

        cur_val = _text_param_value(p) if is_text else (_format_expr_2dec(p.expression) or p.expression or '')
        val_lbl = inputs.addTextBoxCommandInput(val_id, '', cur_val, 1, True)
        tbl.addCommandInput(val_lbl, row, 2)

        txt = inputs.addStringValueInput(txt_id, '', txt_initial)
        txt.tooltip = 'Semicolon-separated values or range, e.g.: 1; 5.5; 12  or  001..099'
        tbl.addCommandInput(txt, row, 3)

        _param_registry[chk_id] = {'param': p, 'text_id': txt_id, 'name': p.name, 'order': row, 'is_text': is_text}
        _param_order.append(p.name)
        row += 1

    if row == 1:
        info = inputs.addTextBoxCommandInput('no_simple_params', '', 'No non-formula parameters found.', 1, True)
        tbl.addCommandInput(info, 1, 0)

def _get_selected_objects(inputs: adsk.core.CommandInputs):
    """Return list of tuples: [('body'|'component', obj, display_name)] based on table checkboxes."""
    selected = []
    for chk_id, (kind, ref) in _item_registry.items():
        chk = adsk.core.BoolValueCommandInput.cast(inputs.itemById(chk_id))
        if chk and chk.value:
            if kind == 'body':
                name = getattr(ref, 'name', 'Body')
            else:  # component (occurrence)
                comp = getattr(ref, 'component', None)
                name = getattr(comp, 'name', 'Component')
            selected.append((kind, ref, name))
    return selected

def _parse_values_list(raw: str, is_text: bool):
    if not raw:
        raise ValueError('empty')
    vals = []
    for tok in (t.strip() for t in raw.split(';')):
        if not tok:
            continue
        expanded = _expand_range(tok)
        if expanded is not None:
            if not is_text:
                for v in expanded:
                    float(v)
            vals.extend(expanded)
        elif is_text:
            vals.append(tok.strip("'").strip('"'))
        else:
            float(tok)
            vals.append(tok)
    if not vals:
        raise ValueError('no values')
    return vals

def _get_selected_params_and_values(inputs: adsk.core.CommandInputs):
    ordered_names = []
    values_map = {}
    is_text_map = {}
    for pname in _param_order:
        meta = None
        chk_id = None
        for cid, m in _param_registry.items():
            if m['name'] == pname:
                meta = m
                chk_id = cid
                break
        if not meta:
            continue
        chk = adsk.core.BoolValueCommandInput.cast(inputs.itemById(chk_id))
        if chk and chk.value:
            txt = adsk.core.StringValueCommandInput.cast(inputs.itemById(meta['text_id']))
            is_text = meta.get('is_text', False)
            vals = _parse_values_list((txt.value or '').strip(), is_text)
            ordered_names.append(pname)
            values_map[pname] = vals
            is_text_map[pname] = is_text
    return ordered_names, values_map, is_text_map

def _validate_filename_template(template: str, selected_param_names, fmt_ext: str):
    """Ensure {name} present and placeholders for every selected parameter. Return normalized template."""
    if not template:
        return False, 'Filename template is empty.'
    if '{name}' not in template:
        return False, 'Filename template must include {name}.'
    missing = [p for p in selected_param_names if f'{{{p}}}' not in template]
    if missing:
        return False, 'Filename template is missing placeholders: ' + ', '.join(f'{{{p}}}' for p in missing)
    # basic ext check (optional; template already includes extension)
    if not template.lower().endswith('.' + fmt_ext.lower()):
        # tolerate custom extension, but you can enforce if you want:
        pass
    return True, ''

def _sanitize_filename_component(s: str):
    """Remove characters illegal on common filesystems."""
    return ''.join(c for c in s if c not in '\\/:*?"<>|\n\r\t').strip()

def _build_filename(template: str, obj_name: str, param_values_map: dict):
    out = template.replace('{name}', _sanitize_filename_component(obj_name))
    for pname, val in param_values_map.items():
        out = out.replace(f'{{{pname}}}', _sanitize_filename_component(str(val)))
    return out

def _normalize_path(p: str) -> str:
    # strip quotes/spaces, expand ~, resolve . and ..
    return os.path.normpath(os.path.expanduser(p.strip().strip('"').strip("'")))

def _validate_all(inputs: adsk.core.CommandInputs):
    # objects
    objs = _get_selected_objects(inputs)
    if not objs:
        return False, 'Select at least one body or component.'

    # params
    try:
        sel_param_names, param_values, _ = _get_selected_params_and_values(inputs)
    except ValueError:
        return False, 'Selected parameters need semicolon-separated values (numeric for non-text params).'
    if not sel_param_names:
        return False, 'Select at least one parameter.'

    # format + template
    fmt = adsk.core.DropDownCommandInput.cast(inputs.itemById(FORMAT_ID))
    fmt_name = fmt.selectedItem.name if (fmt and fmt.selectedItem) else 'OBJ'
    ext = _EXT_MAP.get(fmt_name, 'obj')

    tmpl_in = adsk.core.StringValueCommandInput.cast(inputs.itemById(FILENAME_ID))
    template = (tmpl_in.value or '').strip() if tmpl_in else ''
    ok, msg = _validate_filename_template(template, sel_param_names, ext)
    if not ok:
        return False, msg

    # output dir
    out_dir_in = adsk.core.StringValueCommandInput.cast(inputs.itemById(OUTPUT_DIR_ID))
    raw = (out_dir_in.value or '') if out_dir_in else ''
    out_dir = _normalize_path(raw)

    if not out_dir or not os.path.isdir(out_dir):
        return False, 'Output folder must exist.'
    if not os.access(out_dir, os.W_OK):
        return False, 'Output folder is not writable.'

    return True, ''

def _set_user_params(design, values_tuple, ordered_names, is_text_map, originals_cache):
    ups = design.userParameters
    for pname, pval in zip(ordered_names, values_tuple):
        up = ups.itemByName(pname)
        if not up:
            continue
        if pname not in originals_cache:
            originals_cache[pname] = up.expression
        if is_text_map.get(pname, False):
            up.expression = "'" + str(pval).replace("'", '') + "'"
        else:
            unit = (up.unit or '').strip()
            up.expression = f'{pval} {unit}'.strip()
    return True

def _restore_user_params(design, originals_cache):
    ups = design.userParameters
    for pname, expr in originals_cache.items():
        up = ups.itemByName(pname)
        if up:
            up.expression = expr

def _compute(design):
    try:
        design.computeAll()  # Force recompute (same as Compute All)
    except:
        pass  # don’t hard-fail; export might still succeed

def _snapshot_visibility(design):
    """Return dicts of initial visibility for bodies and occurrences."""
    root = design.rootComponent
    body_vis = {}
    for i in range(root.bRepBodies.count):
        b = root.bRepBodies.item(i)
        body_vis[b.entityToken] = b.isVisible
    occ_vis = {}
    occs = root.occurrences
    for i in range(occs.count):
        oc = occs.item(i)
        occ_vis[oc.entityToken] = oc.isLightBulbOn
    return body_vis, occ_vis

def _restore_visibility(design, body_vis, occ_vis):
    root = design.rootComponent
    for i in range(root.bRepBodies.count):
        b = root.bRepBodies.item(i)
        vis = body_vis.get(b.entityToken, True)
        try: b.isVisible = vis
        except: pass
    occs = root.occurrences
    for i in range(occs.count):
        oc = occs.item(i)
        vis = occ_vis.get(oc.entityToken, True)
        try: oc.isLightBulbOn = vis
        except: pass

def _isolate_for_step(design, kind, ref):
    """
    Hide everything except the target.
    kind: 'body' or 'component' (occurrence)
    ref:  BRepBody or Occurrence
    """
    root = design.rootComponent
    # Hide all occurrences first.
    for i in range(root.occurrences.count):
        oc = root.occurrences.item(i)
        oc.isLightBulbOn = False
    # Show only the target branch and ensure body visibility for body case.
    if kind == 'component':
        # Turn on only this occurrence (and Fusion will show its bodies as they were)
        ref.isLightBulbOn = True
    else:  # body
        # Show the body’s owning occurrence path
        parent_occ = ref.assemblyContext  # may be None if body is in root
        if parent_occ:
            parent_occ.isLightBulbOn = True
        # Hide all bodies, then show only target body
        for i in range(root.bRepBodies.count):
            b = root.bRepBodies.item(i)
            b.isVisible = False
        ref.isVisible = True

def _export_mesh(design, geometry, fullpath, fmt_name):
    em = design.exportManager
    fmt = fmt_name.upper()
    if fmt == 'STL':
        # STL accepts Body/Occurrence/Component
        opts = em.createSTLExportOptions(geometry, fullpath)
        # Optional: opts.meshRefinement = adsk.fusion.MeshRefinementSettings.MeshRefinementMedium
        return em.execute(opts)
    elif fmt == 'OBJ':
        opts = em.createOBJExportOptions(geometry, fullpath)
        return em.execute(opts)
    elif fmt == '3MF':
        opts = em.createC3MFExportOptions(geometry, fullpath)
        return em.execute(opts)
    else:
        return False

def _export_step(design, fullpath):
    em = design.exportManager
    # Export “whole design”; visibility filtering already isolated the target
    opts = em.createSTEPExportOptions(fullpath)  # no geometry arg -> root
    return em.execute(opts)

def _progress_start(title, maximum):
    dlg = _ui.createProgressDialog()
    # message supports %p (percent), %v (current), %m (min), %t (max)
    dlg.show(title, 'Exporting… %v / %t  (%p%%)', 0, maximum, 0)
    dlg.isBackgroundTranslucent = False
    dlg.cancelButtonText = 'Cancel'
    return dlg

def _progress_update(dlg, current, note=None):
    dlg.progressValue = current
    if note:
        dlg.message = f'Exporting… {current} / {dlg.maximumValue}\n{note}'
    adsk.doEvents()  # keep UI responsive

def _progress_end(dlg):
    try: dlg.hide()
    except: pass