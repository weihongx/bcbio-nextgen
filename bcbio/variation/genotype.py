"""High level parallel SNP and indel calling using multiple variant callers.
"""
import os
import collections
import copy

import toolz as tz

from bcbio import bam, utils
from bcbio.distributed.split import grouped_parallel_split_combine
from bcbio.pipeline import datadict as dd
from bcbio.pipeline import region
from bcbio.variation import gatk, gatkfilter, multi, phasing, ploidy, vfilter

# ## Variant filtration -- shared functionality

def variant_filtration(call_file, ref_file, vrn_files, data):
    """Filter variant calls using Variant Quality Score Recalibration.

    Newer GATK with Haplotype calling has combined SNP/indel filtering.
    """
    caller = data["config"]["algorithm"].get("variantcaller")
    call_file = ploidy.filter_vcf_by_sex(call_file, data)
    if caller in ["freebayes"]:
        return vfilter.freebayes(call_file, ref_file, vrn_files, data)
    elif caller in ["platypus"]:
        return vfilter.platypus(call_file, data)
    elif caller in ["samtools"]:
        return vfilter.samtools(call_file, data)
    elif caller in ["gatk", "gatk-haplotype"]:
        return gatkfilter.run(call_file, ref_file, vrn_files, data)
    # no additional filtration for callers that filter as part of call process
    else:
        return call_file

# ## High level functionality to run genotyping in parallel

def get_variantcaller(data, key="variantcaller", default="gatk"):
    if data.get("align_bam"):
        return tz.get_in(["config", "algorithm", key], data, default)

def combine_multiple_callers(samples):
    """Collapse together variant calls from multiple approaches into single data item with `variants`.
    """
    by_bam = collections.OrderedDict()
    for data in (x[0] for x in samples):
        work_bam = tz.get_in(("combine", "work_bam", "out"), data, data.get("align_bam"))
        jointcaller = tz.get_in(("config", "algorithm", "jointcaller"), data)
        variantcaller = get_variantcaller(data)
        key = (multi.get_batch_for_key(data), work_bam)
        if key not in by_bam:
            by_bam[key] = []
        by_bam[key].append((variantcaller, jointcaller, data))
    out = []
    for callgroup in by_bam.values():
        ready_calls = []
        for variantcaller, jointcaller, data in callgroup:
            if variantcaller:
                cur = data.get("vrn_file_plus", {})
                cur.update({"variantcaller": variantcaller,
                            "vrn_file": data.get("vrn_file_orig") if jointcaller else data.get("vrn_file"),
                            "vrn_file_batch": data.get("vrn_file_batch") if not jointcaller else None,
                            "vrn_stats": data.get("vrn_stats"),
                            "validate": data.get("validate") if not jointcaller else None})
                if jointcaller:
                    cur["population"] = False
                ready_calls.append(cur)
            if jointcaller:
                ready_calls.append({"variantcaller": jointcaller,
                                    "vrn_file": data.get("vrn_file"),
                                    "vrn_file_batch": data.get("vrn_file_batch"),
                                    "validate": data.get("validate"),
                                    "do_upload": False})
            if not jointcaller and not variantcaller:
                ready_calls.append({"variantcaller": "precalled",
                                    "vrn_file": data.get("vrn_file"),
                                    "validate": data.get("validate"),
                                    "do_upload": False})
        final = callgroup[0][-1]
        def orig_variantcaller_order(x):
            try:
                return final["config"]["algorithm"]["orig_variantcaller"].index(x["variantcaller"])
            except ValueError:
                return final["config"]["algorithm"]["orig_jointcaller"].index(x["variantcaller"])
        if len(ready_calls) > 1 and "orig_variantcaller" in final["config"]["algorithm"]:
            final["variants"] = sorted(ready_calls, key=orig_variantcaller_order)
            final["config"]["algorithm"]["variantcaller"] = final["config"]["algorithm"].pop("orig_variantcaller")
            if "orig_jointcaller" in final["config"]["algorithm"]:
                final["config"]["algorithm"]["jointcaller"] = final["config"]["algorithm"].pop("orig_jointcaller")
        else:
            final["variants"] = ready_calls
        final.pop("vrn_file_batch", None)
        final.pop("vrn_file_orig", None)
        final.pop("vrn_file_plus", None)
        final.pop("vrn_stats", None)
        out.append([final])
    return out

def _split_by_ready_regions(ext, file_key, dir_ext_fn):
    """Organize splits based on regions generated by parallel_prep_region.
    """
    def _do_work(data):
        if "region" in data:
            name = data["group"][0] if "group" in data else data["description"]
            out_dir = os.path.join(data["dirs"]["work"], dir_ext_fn(data))
            out_file = os.path.join(out_dir, "%s%s" % (name, ext))
            assert isinstance(data["region"], (list, tuple))
            out_parts = []
            for i, r in enumerate(data["region"]):
                out_region_dir = os.path.join(out_dir, r[0])
                out_region_file = os.path.join(out_region_dir,
                                               "%s-%s%s" % (name, region.to_safestr(r), ext))
                work_bams = []
                for xs in data["region_bams"]:
                    if len(xs) == 1:
                        work_bams.append(xs[0])
                    else:
                        work_bams.append(xs[i])
                for work_bam in work_bams:
                    assert os.path.exists(work_bam), work_bam
                out_parts.append((r, work_bams, out_region_file))
            return out_file, out_parts
        else:
            return None, []
    return _do_work

def _collapse_by_bam_variantcaller(samples):
    """Collapse regions to a single representative by BAM input, variant caller and batch.
    """
    by_bam = collections.OrderedDict()
    for data in (x[0] for x in samples):
        work_bam = utils.get_in(data, ("combine", "work_bam", "out"), data.get("align_bam"))
        variantcaller = get_variantcaller(data)
        if isinstance(work_bam, list):
            work_bam = tuple(work_bam)
        key = (multi.get_batch_for_key(data), work_bam, variantcaller)
        try:
            by_bam[key].append(data)
        except KeyError:
            by_bam[key] = [data]
    out = []
    for grouped_data in by_bam.values():
        cur = grouped_data[0]
        cur.pop("region", None)
        region_bams = cur.pop("region_bams", None)
        if region_bams and len(region_bams[0]) > 1:
            cur.pop("work_bam", None)
        out.append([cur])
    return out

def parallel_variantcall_region(samples, run_parallel):
    """Perform variant calling and post-analysis on samples by region.
    """
    to_process = []
    extras = []
    for data in [x[0] for x in samples]:
        added = False
        for add in handle_multiple_callers(data, "variantcaller", "gatk"):
            added = True
            to_process.append([add])
        if not added:
            data = _handle_precalled(data)
            extras.append([data])
    split_fn = _split_by_ready_regions(".vcf.gz", "work_bam", get_variantcaller)
    samples = _collapse_by_bam_variantcaller(
        grouped_parallel_split_combine(to_process, split_fn,
                                       multi.group_batches, run_parallel,
                                       "variantcall_sample", "concat_variant_files",
                                       "vrn_file", ["region", "sam_ref", "config"]))
    return extras + samples

def _handle_precalled(data):
    """Copy in external pre-called variants fed into analysis.
    """
    if data.get("vrn_file"):
        vrn_file = data["vrn_file"]
        if isinstance(vrn_file, (list, tuple)):
            assert len(vrn_file) == 1
            vrn_file = vrn_file[0]
        precalled_dir = utils.safe_makedir(os.path.join(dd.get_work_dir(data), "precalled"))
        ext = utils.splitext_plus(vrn_file)[-1]
        orig_file = os.path.abspath(vrn_file)
        our_vrn_file = os.path.join(precalled_dir, "%s-precalled%s" % (dd.get_sample_name(data), ext))
        utils.copy_plus(orig_file, our_vrn_file)
        data["vrn_file"] = our_vrn_file
    return data

def handle_multiple_callers(data, key, default=None):
    """Split samples that potentially require multiple variant calling approaches.
    """
    callers = get_variantcaller(data, key, default)
    if isinstance(callers, basestring):
        return [data]
    elif not callers:
        return []
    else:
        out = []
        for caller in callers:
            base = copy.deepcopy(data)
            if not base["config"]["algorithm"].get("orig_%s" % key):
                base["config"]["algorithm"]["orig_%s" % key] = \
                  base["config"]["algorithm"][key]
            base["config"]["algorithm"][key] = caller
            # if splitting by variant caller, also split by jointcaller
            if key == "variantcaller":
                jcallers = get_variantcaller(data, "jointcaller", [])
                if isinstance(jcallers, basestring):
                    jcallers = [jcallers]
                if jcallers:
                    base["config"]["algorithm"]["orig_jointcaller"] = jcallers
                    jcallers = [x for x in jcallers if x.startswith(caller)]
                    if jcallers:
                        base["config"]["algorithm"]["jointcaller"] = jcallers[0]
                    else:
                        base["config"]["algorithm"]["jointcaller"] = False
            out.append(base)
        return out

def get_variantcallers():
    from bcbio.variation import freebayes, cortex, samtools, varscan, mutect, platypus, scalpel, vardict, qsnp
    return {"gatk": gatk.unified_genotyper,
            "gatk-haplotype": gatk.haplotype_caller,
            "mutect2": mutect2.mutect2_caller,
            "freebayes": freebayes.run_freebayes,
            "cortex": cortex.run_cortex,
            "samtools": samtools.run_samtools,
            "varscan": varscan.run_varscan,
            "mutect": mutect.mutect_caller,
            "platypus": platypus.run,
            "scalpel": scalpel.run_scalpel,
            "vardict": vardict.run_vardict,
            "vardict-java": vardict.run_vardict,
            "vardict-perl": vardict.run_vardict,
            "qsnp": qsnp.run_qsnp}

def variantcall_sample(data, region=None, align_bams=None, out_file=None):
    """Parallel entry point for doing genotyping of a region of a sample.
    """
    if out_file is None or not os.path.exists(out_file) or not os.path.lexists(out_file):
        utils.safe_makedir(os.path.dirname(out_file))
        sam_ref = data["sam_ref"]
        config = data["config"]
        caller_fns = get_variantcallers()
        caller_fn = caller_fns[config["algorithm"].get("variantcaller", "gatk")]
        if len(align_bams) == 1:
            items = [data]
        else:
            items = multi.get_orig_items(data)
            assert len(items) == len(align_bams)
        call_file = "%s-raw%s" % utils.splitext_plus(out_file)
        assoc_files = tz.get_in(("genome_resources", "variation"), data, {})
        if not assoc_files: assoc_files = {}
        for bam_file in align_bams:
            bam.index(bam_file, data["config"], check_timestamp=False)
        call_file = caller_fn(align_bams, items, sam_ref, assoc_files, region, call_file)
        if data["config"]["algorithm"].get("phasing", False) == "gatk":
            call_file = phasing.read_backed_phasing(call_file, align_bams, sam_ref, region, config)
        utils.symlink_plus(call_file, out_file)
    if region:
        data["region"] = region
    data["vrn_file"] = out_file
    return [data]
