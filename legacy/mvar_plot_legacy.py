from collections import namedtuple
from copy import copy
from itertools import permutations, chain
import random
import csv
from io import StringIO
from PIL import Image, ImageFont, ImageDraw
from fonts.ttf import Roboto
import numpy as np

import modules.scripts as scripts
import gradio as gr

from modules import images, sd_samplers
from modules.hypernetworks import hypernetwork
from modules.processing import process_images, Processed, StableDiffusionProcessingTxt2Img
from modules.shared import opts, cmd_opts, state
import modules.shared as shared
import modules.sd_samplers
import modules.sd_models
import re


def apply_field(field):
    def fun(p, x, xs):
        setattr(p, field, x)

    return fun


def apply_prompt(p: StableDiffusionProcessingTxt2Img, x, xs):
    if xs[0] not in p.prompt and xs[0] not in p.negative_prompt:
        raise RuntimeError(f"Prompt S/R did not find {xs[0]} in prompt or negative prompt.")

    p.prompt = p.prompt.replace(xs[0], x)
    p.negative_prompt = p.negative_prompt.replace(xs[0], x)


def apply_order(p, x, xs):
    token_order = []

    # Initally grab the tokens from the prompt, so they can be replaced in order of earliest seen
    for token in x:
        token_order.append((p.prompt.find(token), token))

    token_order.sort(key=lambda t: t[0])

    prompt_parts = []

    # Split the prompt up, taking out the tokens
    for _, token in token_order:
        n = p.prompt.find(token)
        prompt_parts.append(p.prompt[0:n])
        p.prompt = p.prompt[n + len(token):]

    # Rebuild the prompt with the tokens in the order we want
    prompt_tmp = ""
    for idx, part in enumerate(prompt_parts):
        prompt_tmp += part
        prompt_tmp += x[idx]
    p.prompt = prompt_tmp + p.prompt


def apply_sampler(p, x, xs):
    sampler_name = sd_samplers.samplers_map.get(x.lower(), None)
    if sampler_name is None:
        raise RuntimeError(f"Unknown sampler: {x}")

    p.sampler_name = sampler_name


def confirm_samplers(p, xs):
    for x in xs:
        if x.lower() not in sd_samplers.samplers_map:
            raise RuntimeError(f"Unknown sampler: {x}")


def apply_checkpoint(p, x, xs):
    info = modules.sd_models.get_closet_checkpoint_match(x)
    if info is None:
        raise RuntimeError(f"Unknown checkpoint: {x}")
    modules.sd_models.reload_model_weights(shared.sd_model, info)
    p.sd_model = shared.sd_model


def confirm_checkpoints(p, xs):
    for x in xs:
        if modules.sd_models.get_closet_checkpoint_match(x) is None:
            raise RuntimeError(f"Unknown checkpoint: {x}")


def apply_hypernetwork(p, x, xs):
    if x.lower() in ["", "none"]:
        name = None
    else:
        name = hypernetwork.find_closest_hypernetwork_name(x)
        if not name:
            raise RuntimeError(f"Unknown hypernetwork: {x}")
    hypernetwork.load_hypernetwork(name)


def apply_hypernetwork_strength(p, x, xs):
    hypernetwork.apply_strength(x)


def confirm_hypernetworks(p, xs):
    for x in xs:
        if x.lower() in ["", "none"]:
            continue
        if not hypernetwork.find_closest_hypernetwork_name(x):
            raise RuntimeError(f"Unknown hypernetwork: {x}")


def apply_clip_skip(p, x, xs):
    opts.data["CLIP_stop_at_last_layers"] = x


def format_value_add_label(p, opt, x):
    if type(x) == float:
        x = round(x, 8)

    return f"{opt.label}: {x}"


def format_value(p, opt, x):
    if type(x) == float:
        x = round(x, 8)
    return x


def format_value_join_list(p, opt, x):
    return ", ".join(x)


def do_nothing(p, x, xs):
    pass


def format_nothing(p, opt, x):
    return ""


def str_permutations(x):
    """dummy function for specifying it in AxisOption's type when you want to get a list of permutations"""
    return x

AxisOption = namedtuple("AxisOption", ["label", "type", "apply", "format_value", "confirm"])
AxisOptionImg2Img = namedtuple("AxisOptionImg2Img", ["label", "type", "apply", "format_value", "confirm"])


axis_options = [
    AxisOption("Nothing", str, do_nothing, format_nothing, None),
    AxisOption("Seed", int, apply_field("seed"), format_value_add_label, None),
    AxisOption("Var. seed", int, apply_field("subseed"), format_value_add_label, None),
    AxisOption("Var. strength", float, apply_field("subseed_strength"), format_value_add_label, None),
    AxisOption("Steps", int, apply_field("steps"), format_value_add_label, None),
    AxisOption("CFG Scale", float, apply_field("cfg_scale"), format_value_add_label, None),
    AxisOption("Prompt S/R", str, apply_prompt, format_value, None),
    AxisOption("Prompt order", str_permutations, apply_order, format_value_join_list, None),
    AxisOption("Sampler", str, apply_sampler, format_value, confirm_samplers),
    AxisOption("Checkpoint name", str, apply_checkpoint, format_value, confirm_checkpoints),
    AxisOption("Hypernetwork", str, apply_hypernetwork, format_value, confirm_hypernetworks),
    AxisOption("Hypernet str.", float, apply_hypernetwork_strength, format_value_add_label, None),
    AxisOption("Sigma Churn", float, apply_field("s_churn"), format_value_add_label, None),
    AxisOption("Sigma min", float, apply_field("s_tmin"), format_value_add_label, None),
    AxisOption("Sigma max", float, apply_field("s_tmax"), format_value_add_label, None),
    AxisOption("Sigma noise", float, apply_field("s_noise"), format_value_add_label, None),
    AxisOption("Eta", float, apply_field("eta"), format_value_add_label, None),
    AxisOption("Clip skip", int, apply_clip_skip, format_value_add_label, None),
    AxisOption("Denoising", float, apply_field("denoising_strength"), format_value_add_label, None),
    AxisOption("Cond. Image Mask Weight", float, apply_field("inpainting_mask_weight"), format_value_add_label, None),
]

def draw_crp_pages(p: StableDiffusionProcessingTxt2Img, row_field_values, col_field_values, page_field_values, col_labels, row_labels, page_labels, cell, draw_legend, include_lone_images) -> Processed:
    """ Draw image grids on multiple images based on options chosen by the user

    Args:
        p (`StableDiffusionProcessingTxt2Img`)
        row_field_values, col_field_values, page_field_values (`list(Any)`):
            Values after being processed by `process_axis`
        col_labels, row_labels, page_labels (`list`):
            Column values formatted for printing on the grid

    Returns: (result)
        result (`PIL.Image`):
            Image with `title_text` drawn above `im`
    """

    state.job_count = len(col_field_values) * len(row_field_values) * len(page_field_values) * p.n_iter

    col_texts = [[images.GridAnnotation(c)] for c in col_labels]
    row_texts = [[images.GridAnnotation(r)] for r in row_labels]
    page_texts = [[images.GridAnnotation(pg)] for pg in page_labels]

    # Temporary list of all the images that are generated to be populated into each grid.
    # Will be filled with empty images for any individual step that fails to process properly.
    # Cleared after each grid generation.
    cache_images = []

    processed_result = None
    cell_mode = "P"
    cell_size = (1,1)

    for ipg, pg in enumerate(page_field_values):
        for ir, r in enumerate(row_field_values):
            for ic, c in enumerate(col_field_values):
                state.job = f"{ic + ( ir * ipg ) * len(col_field_values) + 1} out of {len(col_field_values) * len(row_field_values) * len(page_field_values)}"

                processed:Processed = cell(c, r, pg)

                try:
                    # this dereference will throw an exception if the image was not processed
                    # (this happens in cases such as if the user stops the process from the UI)
                    processed_image = processed.images[0]

                    if processed_result is None:
                        # Use our first valid processed result as a template container to hold our full results
                        processed_result = copy(processed)
                        cell_mode = processed_image.mode
                        cell_size = processed_image.size

                        # Clear out that first image to prevent duplicates
                        processed_result.images.clear()
                        processed_result.all_prompts.clear()
                        processed_result.all_seeds.clear()
                        processed_result.infotexts.clear()

                    if include_lone_images:
                        processed_result.images.append(processed_image)
                        processed_result.all_prompts.append(processed.prompt)
                        processed_result.all_seeds.append(processed.seed)
                        processed_result.infotexts.append(processed.infotexts[0])

                except:
                    processed_image = Image.new(cell_mode, cell_size)

                cache_images.append(processed_image)

                # cascade out if interrupted and fill the remainder of the page with blank images
                # this is to get out of the script faster when the user has selected the "Checkpoint Name" module
                if state.interrupted: break
            if state.interrupted:
                for i in range(len(cache_images), len(col_field_values) * len(row_field_values)):
                    cache_images.append(Image.new(cell_mode, cell_size))
                break

        grid = images.image_grid(cache_images, rows=len(row_field_values))
        cache_images.clear()

        if draw_legend:
            # Draw row and column labels
            grid = images.draw_grid_annotations(grid, cell_size[0], cell_size[1], col_texts, row_texts)

            # Draw page labels
            if len(page_field_values) > 1:
                w, h = grid.size
                empty_string = [[images.GridAnnotation()]]
                grid = images.draw_grid_annotations(grid, w, h, [page_texts[ipg]], empty_string)

        processed_result.images.insert(ipg, grid)
        processed_result.all_prompts.insert(ipg, "")
        processed_result.all_seeds.insert(ipg, -1)
        processed_result.infotexts.insert(ipg, "")

        if state.interrupted: break

    if processed_result is None:
        print("Unexpected error: draw_crp_pages failed to return even a single processed image")
        return Processed(), 0

    return processed_result, ipg + 1

class SharedSettingsStackHelper(object):
    def __enter__(self):
        self.CLIP_stop_at_last_layers = opts.CLIP_stop_at_last_layers
        self.hypernetwork = opts.sd_hypernetwork
        self.model = shared.sd_model
  
    def __exit__(self, exc_type, exc_value, tb):
        modules.sd_models.reload_model_weights(self.model)

        hypernetwork.load_hypernetwork(self.hypernetwork)
        hypernetwork.apply_strength()

        opts.data["CLIP_stop_at_last_layers"] = self.CLIP_stop_at_last_layers


re_range = re.compile(r"\s*([+-]?\s*\d+)\s*-\s*([+-]?\s*\d+)(?:\s*\(([+-]\d+)\s*\))?\s*")
re_range_float = re.compile(r"\s*([+-]?\s*\d+(?:.\d*)?)\s*-\s*([+-]?\s*\d+(?:.\d*)?)(?:\s*\(([+-]\d+(?:.\d*)?)\s*\))?\s*")

re_range_count = re.compile(r"\s*([+-]?\s*\d+)\s*-\s*([+-]?\s*\d+)(?:\s*\[(\d+)\s*\])?\s*")
re_range_count_float = re.compile(r"\s*([+-]?\s*\d+(?:.\d*)?)\s*-\s*([+-]?\s*\d+(?:.\d*)?)(?:\s*\[(\d+(?:.\d*)?)\s*\])?\s*")

class Script(scripts.Script):
    def title(self):
        return "MVar Plot Legacy"

    def ui(self, is_img2img : bool):
        current_axis_options = [c for c in axis_options if type(c) == AxisOption or type(c) == AxisOptionImg2Img and is_img2img]

        with gr.Row():
            col_module = gr.Dropdown(label="Col module", choices=[c.label for c in current_axis_options], value=current_axis_options[1].label, type="index", elem_id="c_type")
            col_values = gr.Textbox(label="Col values", lines=1)

        with gr.Row():
            row_module = gr.Dropdown(label="Row module", choices=[r.label for r in current_axis_options], value=current_axis_options[0].label, type="index", elem_id="r_type")
            row_values = gr.Textbox(label="Row values", lines=1)

        with gr.Row():
            page_module = gr.Dropdown(label="Page module", choices=[pg.label for pg in current_axis_options], value=current_axis_options[0].label, type="index", elem_id="pg_type")
            page_values = gr.Textbox(label="Page values", lines=1)
        
        draw_legend = gr.Checkbox(label='Draw legend', value=True)
        include_lone_images = gr.Checkbox(label='Include Separate Images', value=False)
        no_fixed_seeds = gr.Checkbox(label='Keep -1 for seeds', value=False)

        return [col_module, col_values, row_module, row_values, page_module, page_values, draw_legend, include_lone_images, no_fixed_seeds]

    def run(self, p, col_module, col_values, row_module, row_values, page_module, page_values, draw_legend, include_lone_images, no_fixed_seeds):

        if not no_fixed_seeds:
            modules.processing.fix_seed(p)

        if not opts.return_grid:
            p.batch_size = 1

        def process_axis(opt, vals):
            if opt.label == 'Nothing':
                return [0]

            valslist = [x.strip() for x in chain.from_iterable(csv.reader(StringIO(vals)))]

            if opt.type == int:
                valslist_ext = []

                for val in valslist:
                    m = re_range.fullmatch(val)
                    mc = re_range_count.fullmatch(val)
                    if m is not None:
                        start = int(m.group(1))
                        end = int(m.group(2))+1
                        step = int(m.group(3)) if m.group(3) is not None else 1

                        valslist_ext += list(range(start, end, step))
                    elif mc is not None:
                        start = int(mc.group(1))
                        end   = int(mc.group(2))
                        num   = int(mc.group(3)) if mc.group(3) is not None else 1
                        
                        valslist_ext += [int(x) for x in np.linspace(start=start, stop=end, num=num).tolist()]
                    else:
                        valslist_ext.append(val)

                valslist = valslist_ext
            elif opt.type == float:
                valslist_ext = []

                for val in valslist:
                    m = re_range_float.fullmatch(val)
                    mc = re_range_count_float.fullmatch(val)
                    if m is not None:
                        start = float(m.group(1))
                        end = float(m.group(2))
                        step = float(m.group(3)) if m.group(3) is not None else 1

                        valslist_ext += np.arange(start, end + step, step).tolist()
                    elif mc is not None:
                        start = float(mc.group(1))
                        end   = float(mc.group(2))
                        num   = int(mc.group(3)) if mc.group(3) is not None else 1
                        
                        valslist_ext += np.linspace(start=start, stop=end, num=num).tolist()
                    else:
                        valslist_ext.append(val)

                valslist = valslist_ext
            elif opt.type == str_permutations:
                valslist = list(permutations(valslist))

            valslist = [opt.type(x) for x in valslist]

            # Confirm options are valid before starting
            if opt.confirm:
                opt.confirm(p, valslist)

            return valslist

        col_options = axis_options[col_module]
        col_field_values = process_axis(col_options, col_values)

        row_options = axis_options[row_module]
        row_field_values = process_axis(row_options, row_values)

        page_options = axis_options[page_module]
        page_field_values = process_axis(page_options, page_values)

        def fix_axis_seeds(axis_opt, axis_list):
            if axis_opt.label in ['Seed','Var. seed']:
                return [int(random.randrange(4294967294)) if val is None or val == '' or val == -1 else val for val in axis_list]
            else:
                return axis_list

        if not no_fixed_seeds:
            col_field_values = fix_axis_seeds(col_options, col_field_values)
            row_field_values = fix_axis_seeds(row_options, row_field_values)
            page_field_values = fix_axis_seeds(page_options, page_field_values)

        if col_options.label == 'Steps':
            total_steps = sum(col_field_values) * len(row_field_values) * len(page_field_values)
        elif row_options.label == 'Steps':
            total_steps = sum(row_field_values) * len(col_field_values) * len(page_field_values)
        elif page_options.label == 'Steps':
            total_steps = sum(page_field_values) * len(row_field_values) * len(col_field_values)
        else:
            total_steps = p.steps * len(row_field_values) * len(col_field_values) * len(page_field_values)

        if isinstance(p, StableDiffusionProcessingTxt2Img) and p.enable_hr:
            total_steps *= 2

        print(f"MVar plot legacy will create {len(col_field_values) * len(row_field_values) * len(page_field_values) * p.n_iter} images on {len(page_field_values)} {len(col_field_values)}x{len(row_field_values)} pages. (Total steps to process: {total_steps * p.n_iter})")
        shared.total_tqdm.updateTotal(total_steps * p.n_iter)

        def cell(col_value, row_value, page_value):
            pc = copy(p)
            col_options.apply(pc, col_value, col_field_values)
            row_options.apply(pc, row_value, row_field_values)
            page_options.apply(pc, page_value, page_field_values)

            return process_images(pc)

        with SharedSettingsStackHelper():
            processed, page_count = draw_crp_pages(
                p,
                col_field_values=col_field_values,
                row_field_values=row_field_values,
                page_field_values=page_field_values,
                col_labels=[col_options.format_value(p, col_options, col_value) for col_value in col_field_values],
                row_labels=[row_options.format_value(p, row_options, row_value) for row_value in row_field_values],
                page_labels=[page_options.format_value(p, page_options, page_value) for page_value in page_field_values],
                cell=cell,
                draw_legend=draw_legend,
                include_lone_images=include_lone_images
            )

            if opts.grid_save:
                for ipage_count in range(0,page_count):
                    images.save_image(processed.images[ipage_count], p.outpath_grids, "mvar_plot_grid", extension=opts.grid_format, prompt=p.prompt, seed=processed.seed, grid=True, p=p)

        return processed