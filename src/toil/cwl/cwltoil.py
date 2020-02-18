""" Implement support for Common Workflow Language (CWL) for Toil."""
#
# Copyright (C) 2015 Curoverse, Inc
# Copyright (C) 2016 UCSC Computational Genomics Lab
# Copyright (C) 2019 Seven Bridges
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# For an overview of how this all works, see discussion in
# docs/architecture.rst

from builtins import range
from builtins import object
import abc
import argparse
import os
import tempfile
import json
import sys
import logging
import copy
import functools
import shutil
from typing import Text, Mapping, MutableSequence, MutableMapping
import hashlib
import uuid
import datetime

# Python 3 compatibility imports
from six import iteritems, string_types
from six.moves.urllib import parse as urlparse
import six

from schema_salad import validate
from schema_salad.schema import Names
import schema_salad.ref_resolver

import cwltool.errors
import cwltool.load_tool
import cwltool.main
import cwltool.workflow
import cwltool.expression
import cwltool.builder
import cwltool.resolver
import cwltool.stdfsaccess
import cwltool.command_line_tool
import cwltool.provenance

from toil.jobStores.abstractJobStore import NoSuchJobStoreException
from toil.fileStores import FileID
from cwltool.loghandler import _logger as cwllogger
from cwltool.loghandler import defaultStreamHandler
from cwltool.pathmapper import (PathMapper, adjustDirObjs, adjustFileObjs,
                                get_listing, MapperEnt, visit_class,
                                normalizeFilesDirs)
from cwltool.process import (shortname, fill_in_defaults, compute_checksums,
                             add_sizes)
from cwltool.secrets import SecretStore
from cwltool.software_requirements import (
    DependenciesConfiguration, get_container_from_software_requirements)
from cwltool.utils import aslist
from cwltool.mutation import MutationManager

from toil.job import Job, Promise
from toil.common import Config, Toil, addOptions
from toil.version import baseVersion

# Define internal jobs we should avoid submitting to batch systems and logging
CWL_INTERNAL_JOBS = ("CWLJobWrapper", "CWLWorkflow", "CWLScatter", "CWLGather",
                     "ResolveIndirect")

# The job object passed into CWLJob and CWLWorkflow
# is a dict mapping to tuple of (key, dict)
# the final dict is derived by evaluating each
# tuple looking up the key in the supplied dict.
#
# This is necessary because Toil jobs return a single value (a dict)
# but CWL permits steps to have multiple output parameters that may
# feed into multiple other steps.  This transformation maps the key in the
# output object to the correct key of the input object.


class IndirectDict(dict):
    """Tag to indicate a dict is an IndirectDict that needs to resolved."""
    pass


class SkipNull:
    """
    Internal sentinel object that indicates a null value produced by each port of a skipped
    conditional step. The CWL 1.2 specification calls for treating this the exact same
    as a null value.
    """
    pass


def filter_skip_null(name, value):
    """
    Recursively filter out SkipNull objects from 'value'.

    :param name: Name of port producing this value.
                 Only used when we find an unhandled null from a conditional step
                 and we print out a warning. The name allows the user to better
                 localize which step/port was responsible for the unhandled null.
    :param value: port output value object
    """
    err_flag = [False]
    value = _filter_skip_null(name, value, err_flag)
    if err_flag[0]:
        cwllogger.warning(
            "In %s, SkipNull result found and cast to None. \n"
            "You had a conditional step that did not run, "
            "but you did not use pickValue to handle the skipped input." % name)
    return value


def _filter_skip_null(name, value, err_flag):
    """
    Private implementation for recursively filtering out SkipNull objects from 'value'.

    :param name: Name of port producing this value.
                 Only used when we find an unhandled null from a conditional step
                 and we print out a warning. The name allows the user to better
                 localize which step/port was responsible for the unhandled null.
    :param value: port output value object
    :param err_flag: A pass by reference boolean (passed by enclosing in a list) that
                     allows us to flag, at any level of recursion, that we have encountered
                     a SkipNull.
    """
    if isinstance(value, SkipNull):
        err_flag[0] = True
        value = None
    elif isinstance(value, list):
        return [_filter_skip_null(name, v, err_flag) for v in value]
    elif isinstance(value, dict):
        return {k: _filter_skip_null(name, v, err_flag) for k, v in value.items()}
    return value


class Conditional:
    """
    Object holding conditional expression until we are ready to evaluate it.
    Evaluation occurs at the moment the encloses step is ready to run.
    """
    def __init__(self, expression=None, outputs=None, requirements=None):
        """
        Instantiate a conditional expression.

        :param expression: A string with the expression from the 'when' field of the step
        :param outputs: The output dictionary for the step. This is needed because if the
                        step is skipped, all the outputs need to be populated with SkipNull
                        values
        :param requirements: The requirements object that is needed for the context the
                             expression will evaluate in.
        """
        self.expression = expression
        self.outputs = outputs
        self.requirements = requirements

    def is_false(self, job):
        """
        Determine if expression evaluates to False given completed step inputs

        :param job: job output object
        :return: bool
        """
        if self.expression is None:
            return False

        expr_is_true = cwltool.expression.do_eval(
            self.expression,
            {shortname(k): v for k, v in iteritems(
                resolve_indirect(job))},
            self.requirements,
            None,
            None,
            {})

        if isinstance(expr_is_true, bool):
            return not expr_is_true

        raise cwltool.errors.WorkflowException(
                "'%s' evaluated to a non-boolean value" % self.expression)

    def skipped_outputs(self):
        """
        Generate a dictionary of SkipNull objects corresponding to the output
        structure of the step.

        :return: dict
        """
        outobj = {}

        def sn(n):
            if isinstance(n, Mapping):
                return shortname(n["id"])
            if isinstance(n, string_types):
                return shortname(n)

        for k in [sn(o) for o in self.outputs]:
            outobj[k] = SkipNull()

        return outobj


class ResolveSource(object):
    """
    Apply linkMerge and pickValue operators to values coming into a port.
    """
    def __init__(self, name, input, source_key, promises):
        """
        Construct a container object that carries what information it can about the input
        sources and the current promises, ready for evaluation when the time comes.

        :param name: human readable name of step/port that this value refers to
        :param input: CWL input object complete with linkMerge and pickValue fields
        :param source_key: "source" or "outputSource" depending on what it is
        :param promises: incident values packed as promises
        """
        self.name, self.input, self.source_key = name, input, source_key

        source_names = aslist(self.input[self.source_key])
        # Rule is that source: [foo] is just foo unless it also has linkMerge: merge_nested
        if input.get("linkMerge") or len(source_names) > 1:
            self.promise_tuples = [
                (shortname(s), promises[s].rv())
                for s in source_names
            ]
        else:
            # KG: Cargo culting this logic and the reason given from original Toil code:
            # It seems that an input source with a
            # '#' in the name will be returned as a
            # CommentedSeq list by the yaml parser.
            s = str(source_names[0])
            self.promise_tuples = (shortname(s), promises[s].rv())

    def resolve(self):
        """
        First apply linkMerge then pickValue if either present

        :return: dict
        """
        if isinstance(self.promise_tuples, list):
            result = self.link_merge([v[1][v[0]] for v in self.promise_tuples])
        else:
            value = self.promise_tuples
            result = value[1].get(value[0])

        result = self.pick_value(result)
        result = filter_skip_null(self.name, result)
        return result

    def link_merge(self, values):
        """
        Apply linkMerge operator to `values` object

        :param values: dict: result of step
        :return: dict
        """
        link_merge_type = self.input.get("linkMerge", "merge_nested")

        if link_merge_type == "merge_nested":
            return values

        elif link_merge_type == "merge_flattened":
            result = []
            for v in values:
                if isinstance(v, MutableSequence):
                    result.extend(v)
                else:
                    result.append(v)
            return result

        else:
            raise validate.ValidationException(
                "Unsupported linkMerge '%s' on %s." % (link_merge_type, self.name))

    def pick_value(self, values):
        """
        Apply pickValue operator to `values` object

        :param values: dict
        :return:
        """
        pick_value_type = self.input.get("pickValue")

        if pick_value_type is None:
            return values

        if not isinstance(values, list):
            cwllogger.warning(
                "pickValue used but input %s is not a list." % self.name)
            return values

        result = [v for v in values if not isinstance(v, SkipNull) and v is not None]

        if pick_value_type == "first_non_null":
            if len(result) < 1:
                raise cwltool.errors.WorkflowException(
                    "%s: first_non_null operator found no non-null values" % self.name)
            else:
                return result[0]

        elif pick_value_type == "only_non_null":
            if len(result) == 0:
                raise cwltool.errors.WorkflowException(
                    "%s: only_non_null operator found no non-null values" % self.name)
            elif len(result) > 1:
                raise cwltool.errors.WorkflowException(
                    "%s: only_non_null operator found more than one non-null values" % self.name)
            else:
                return result[0]

        elif pick_value_type == "all_non_null":
            return result

        else:
            raise cwltool.errors.WorkflowException(
                "Unsupported pickValue '%s' on %s" % (pick_value_type, self.name))


class StepValueFrom(object):
    """
    A workflow step input which has a valueFrom expression attached to it, which
    is evaluated to produce the actual input object for the step.
    """
    def __init__(self, expr, source, req):
        """
        Instantiate an object to carry as much information as we have access to right now
        about this valueFrom expression

        :param expr: str: expression as a string
        :param source: the source promise of this step
        :param req: requirements object that is consumed by CWLtool expression evaluator
        """
        self.expr = expr
        self.source = source
        self.context = None
        self.req = req

    def resolve(self):
        """
        The valueFrom expression's context is the value for this input. Resolve the promise.

        :return: object that will serve as expression context
        """
        self.context = self.source.resolve()
        return self.context

    def do_eval(self, inputs):
        """
        Evaluate the valueFrom expression with the given input object

        :param inputs:
        :return: object
        """
        return cwltool.expression.do_eval(
            self.expr, inputs, self.req, None, None, {}, context=self.context)


class DefaultWithSource(object):
    """
    A workflow step input that has both a source and a default value.
    """
    def __init__(self, default, source):
        """
        Instantiate an object to handle a source that has a default value

        :param default: the default value
        :param source: the source object
        """
        self.default = default
        self.source = source

    def resolve(self):
        """
        Determine the final input value when the time is right (when the source can be resolved)

        :return: dict
        """
        if self.source:
            result = self.source.resolve()
            if result:
                return result
        return self.default


class JustAValue(object):
    """
    A simple value masquerading as a 'resolve'-able object
    """

    def __init__(self, val):
        self.val = val

    def resolve(self):
        return self.val


def resolve_indirect(pdict):
    """
    Resolve the contents of an indirect dictionary (containing promises) and
    evaluate expressions to produce the dictionary of actual values.

    :param pdict: input pdict for these values
    :return:
    """
    if isinstance(pdict, IndirectDict):
        first_pass_results = {k: v.resolve() for k, v in pdict.items()}
    else:
        first_pass_results = {k: v for k, v in pdict.items()}

    result = {}
    for k, v in pdict.items():
        if isinstance(v, StepValueFrom):
            result[k] = v.do_eval(inputs=first_pass_results)
        else:
            result[k] = first_pass_results[k]
    return result


def simplify_list(maybe_list):
    """Turn a length one list loaded by cwltool into a scalar.
    Anything else is passed as-is, by reference."""
    if isinstance(maybe_list, MutableSequence):
        is_list = aslist(maybe_list)
        if len(is_list) == 1:
            return is_list[0]
    return maybe_list


class ToilPathMapper(PathMapper):
    """
    ToilPathMapper keeps track of a file's symbolic identifier (the Toil
    FileID), its local path on the host (the value returned by readGlobalFile)
    and the the location of the file inside the Docker container.
    """

    def __init__(self, referenced_files, basedir, stagedir,
                 separateDirs=True,
                 get_file=None,
                 stage_listing=False):
        self.get_file = get_file
        self.stage_listing = stage_listing
        super(ToilPathMapper, self).__init__(
            referenced_files, basedir, stagedir, separateDirs=separateDirs)

    def visit(self, obj, stagedir, basedir, copy=False, staged=False):
        # type: (Dict[Text, Any], Text, Text, bool, bool) -> None
        tgt = os.path.join(stagedir, obj["basename"])
        if obj["location"] in self._pathmap:
            return
        if obj["class"] == "Directory":
            if obj["location"].startswith("file://"):
                resolved = schema_salad.ref_resolver.uri_file_path(
                    obj["location"])
            else:
                resolved = obj["location"]
            self._pathmap[obj["location"]] = MapperEnt(
                resolved, tgt,
                "WritableDirectory" if copy else "Directory", staged)
            if obj["location"].startswith("file://") \
                    and not self.stage_listing:
                staged = False
            self.visitlisting(
                obj.get("listing", []), tgt, basedir, copy=copy, staged=staged)
        elif obj["class"] == "File":
            loc = obj["location"]
            if "contents" in obj and obj["location"].startswith("_:"):
                self._pathmap[obj["location"]] = MapperEnt(
                    obj["contents"], tgt, "CreateFile", staged)
            else:
                resolved = self.get_file(loc) if self.get_file else loc
                if resolved.startswith("file:"):
                    resolved = schema_salad.ref_resolver.uri_file_path(
                        resolved)
                self._pathmap[loc] = MapperEnt(
                    resolved, tgt, "WritableFile" if copy else "File", staged)
                self.visitlisting(obj.get("secondaryFiles", []),
                                  stagedir, basedir, copy=copy, staged=staged)


class ToilCommandLineTool(cwltool.command_line_tool.CommandLineTool):
    """Subclass the cwltool command line tool to provide the custom
    Toil.PathMapper.
    """

    def make_path_mapper(self, reffiles, stagedir, runtimeContext,
                         separateDirs):
        return ToilPathMapper(
            reffiles, runtimeContext.basedir, stagedir, separateDirs,
            runtimeContext.toil_get_file)


def toil_make_tool(toolpath_object, loading_context):
    """Factory function passed to load_tool() which creates instances of the
    custom ToilCommandLineTool.

    """

    if isinstance(toolpath_object, Mapping) \
            and toolpath_object.get("class") == "CommandLineTool":
        return ToilCommandLineTool(toolpath_object, loading_context)
    return cwltool.workflow.default_make_tool(toolpath_object, loading_context)


class ToilFsAccess(cwltool.stdfsaccess.StdFsAccess):
    """Custom filesystem access class which handles toil filestore references.

    """

    def __init__(self, basedir, file_store=None):
        self.file_store = file_store
        super(ToilFsAccess, self).__init__(basedir)

    def _abs(self, path):
        if path.startswith("toilfs:"):
            return self.file_store.readGlobalFile(FileID.unpack(path[7:]))
        return super(ToilFsAccess, self)._abs(path)


def toil_get_file(file_store, index, existing, file_store_id):
    """Get path to input file from Toil jobstore."""

    if not file_store_id.startswith("toilfs:"):
        return file_store.jobStore.getPublicUrl(file_store.jobStore.importFile(file_store_id))
    src_path = file_store.readGlobalFile(FileID.unpack(file_store_id[7:]))
    index[src_path] = file_store_id
    existing[file_store_id] = src_path
    return schema_salad.ref_resolver.file_uri(src_path)


def write_file(writeFunc, index, existing, x):
    """Write a file into the Toil jobstore.

    'existing' is a set of files retrieved as inputs from toil_get_file. This
    ensures they are mapped back as the same name if passed through.

    """
    # Toil fileStore reference
    if x.startswith("toilfs:"):
        return x
    # File literal outputs with no path, we don't write these and will fail
    # with unsupportedRequirement when retrieving later with getFile
    elif x.startswith("_:"):
        return x
    else:
        x = existing.get(x, x)
        if x not in index:
            if not urlparse.urlparse(x).scheme:
                rp = os.path.realpath(x)
            else:
                rp = x
            try:
                index[x] = "toilfs:" + writeFunc(rp).pack()
                existing[index[x]] = x
            except Exception as e:
                cwllogger.error("Got exception '%s' while copying '%s'", e, x)
                raise
        return index[x]


def uploadFile(uploadfunc, fileindex, existing, uf, skip_broken=False):
    """Update a file object so that the location is a reference to the toil file
    store, writing it to the file store if necessary.

    """

    if uf["location"].startswith("toilfs:") or uf["location"].startswith("_:"):
        return
    if uf["location"] in fileindex:
        uf["location"] = fileindex[uf["location"]]
        return
    if not uf["location"] and uf["path"]:
        uf["location"] = schema_salad.ref_resolver.file_uri(uf["path"])
    if uf["location"].startswith("file://") and not os.path.isfile(uf["location"][7:]):
        if skip_broken:
            return
        else:
            raise cwltool.errors.WorkflowException(
                "File is missing: %s" % uf["location"])
    uf["location"] = write_file(
        uploadfunc, fileindex, existing, uf["location"])


def writeGlobalFileWrapper(file_store, fileuri):
    """Wrap writeGlobalFile to accepts file:// URIs"""
    return file_store.writeGlobalFile(
        schema_salad.ref_resolver.uri_file_path(fileuri))


class ResolveIndirect(Job):
    """A helper Job which accepts an indirect dict (containing promises) and
    produces a dictionary of actual values.

    """

    def __init__(self, cwljob):
        super(ResolveIndirect, self).__init__()
        self.cwljob = cwljob

    def run(self, file_store):
        return resolve_indirect(self.cwljob)


def toilStageFiles(file_store, cwljob, outdir, index, existing, export,
                   destBucket=None):
    """Copy input files out of the global file store and update location and
    path."""

    def _collectDirEntries(obj):
    # type: (Union[Dict[Text, Any], List[Dict[Text, Any]]]) -> Iterator[Dict[Text, Any]]
        if isinstance(obj, dict):
            if obj.get("class") in ("File", "Directory"):
                yield obj
            else:
                for sub_obj in obj.values():
                    for dir_entry in _collectDirEntries(sub_obj):
                        yield dir_entry
        elif isinstance(obj, list):
            for sub_obj in obj:
                for dir_entry in _collectDirEntries(sub_obj):
                    yield dir_entry

    jobfiles = list(_collectDirEntries(cwljob))
    pm = ToilPathMapper(
        jobfiles, "", outdir, separateDirs=False, stage_listing=True)
    for _, p in pm.items():
        if not p.staged:
            continue

        # Deal with bucket exports
        if destBucket:
            # Directories don't need to be created if we're exporting to
            # a bucket
            if p.type == "File":
                # Remove the staging directory from the filepath and
                # form the destination URL
                unstageTargetPath = p.target[len(outdir):]
                destUrl = '/'.join(s.strip('/')
                                   for s in [destBucket, unstageTargetPath])

                file_store.exportFile(FileID.unpack(p.resolved[7:]), destUrl)

            continue

        if not os.path.exists(os.path.dirname(p.target)):
            os.makedirs(os.path.dirname(p.target), 0o0755)
        if p.type == "File":
            file_store.exportFile(FileID.unpack(p.resolved[7:]), "file://" + p.target)
        elif p.type == "Directory" and not os.path.exists(p.target):
            os.makedirs(p.target, 0o0755)
        elif p.type == "CreateFile":
            with open(p.target, "wb") as n:
                n.write(p.resolved.encode("utf-8"))

    def _check_adjust(f):
        f["location"] = schema_salad.ref_resolver.file_uri(
            pm.mapper(f["location"])[1])
        if "contents" in f:
            del f["contents"]
        return f

    visit_class(cwljob, ("File", "Directory"), _check_adjust)


class CWLJobWrapper(Job):
    """Wrap a CWL job that uses dynamic resources requirement.  When executed, this
    creates a new child job which has the correct resource requirement set.

    """

    def __init__(self, tool, cwljob, runtime_context, conditional):
        super(CWLJobWrapper, self).__init__(
            cores=1, memory=1024*1024, disk=8*1024)
        self.cwltool = remove_pickle_problems(tool)
        self.cwljob = cwljob
        self.runtime_context = runtime_context
        self.conditional = conditional

    def run(self, file_store):
        cwljob = resolve_indirect(self.cwljob)
        fill_in_defaults(
            self.cwltool.tool['inputs'], cwljob,
            self.runtime_context.make_fs_access(
                self.runtime_context.basedir or ""))
        realjob = CWLJob(self.cwltool, cwljob, self.runtime_context, conditional=self.conditional)
        self.addChild(realjob)
        return realjob.rv()


class CWLJob(Job):
    """Execute a CWL tool using cwltool.executors.SingleJobExecutor"""

    def __init__(self, tool, cwljob, runtime_context, step_inputs=None, conditional=None):
        self.cwltool = remove_pickle_problems(tool)
        self.conditional = conditional or Conditional()

        if runtime_context.builder:
            builder = runtime_context.builder
        else:
            builder = cwltool.builder.Builder(
                job=cwljob,
                files=[],
                bindings=[],
                schemaDefs={},
                names=Names(),
                requirements=self.cwltool.requirements,
                hints=[],
                resources={},
                mutation_manager=None,
                formatgraph=None,
                make_fs_access=runtime_context.make_fs_access,
                fs_access=runtime_context.make_fs_access(''),
                job_script_provider=None,
                timeout=runtime_context.eval_timeout,
                debug=False,
                js_console=False,
                force_docker_pull=False,
                loadListing=u'',
                outdir=u'',
                tmpdir=u'',
                stagedir=u''
            )
        req = tool.evalResources(builder, runtime_context)
        # pass the default of None if basecommand is empty
        unitName = self.cwltool.tool.get("baseCommand", None)
        if isinstance(unitName, (MutableSequence, tuple)):
            unitName = ' '.join(unitName)

        try:
            displayName = str(self.cwltool.tool['id'])
        except KeyError:
            displayName = None

        super(CWLJob, self).__init__(
            cores=req["cores"], memory=int(req["ram"]*(2**20)),
            disk=int((req["tmpdirSize"]*(2**20))+(req["outdirSize"]*(2**20))),
            unitName=unitName,
            displayName=displayName)

        self.cwljob = cwljob
        try:
            self.jobName = str(self.cwltool.tool['id'])
        except KeyError:
            # fall back to the Toil defined class name if the tool doesn't have
            # an identifier
            pass
        self.runtime_context = runtime_context
        self.step_inputs = step_inputs or self.cwltool.tool["inputs"]
        self.workdir = runtime_context.workdir

    def run(self, file_store):
        cwljob = resolve_indirect(self.cwljob)
        fill_in_defaults(
            self.step_inputs, cwljob,
            self.runtime_context.make_fs_access(""))

        if self.conditional.is_false(cwljob):
            return self.conditional.skipped_outputs()

        immobile_cwljob_dict = copy.deepcopy(cwljob)
        logging.error('CWL Job: %s' % json.dumps(immobile_cwljob_dict, indent=4))
        for inp_id in immobile_cwljob_dict.keys():
            found = False
            for field in self.cwltool.inputs_record_schema['fields']:
                if field['name'] == inp_id:
                    found = True
            if not found:
                cwljob.pop(inp_id)

        # Exports temporary directory for batch systems that reset TMPDIR
        os.environ["TMPDIR"] = os.path.realpath(file_store.getLocalTempDir())
        outdir = os.path.join(file_store.getLocalTempDir(), "out")
        os.mkdir(outdir)
        # Just keep the temporary output prefix under the job's local temp dir,
        # next to the outdir.
        #
        # If we maintain our own system of nested temp directories, we won't
        # know when all the jobs using a higher-level directory are ready for
        # it to be deleted. The local temp dir, under Toil's workDir, will be
        # cleaned up by Toil.
        tmp_outdir_prefix = os.path.join(file_store.getLocalTempDir(), "tmp-out")

        index = {}
        existing = {}
        # Prepare the run instructions for cwltool
        runtime_context = self.runtime_context.copy()
        runtime_context.basedir = os.getcwd()
        runtime_context.outdir = outdir
        runtime_context.tmp_outdir_prefix = tmp_outdir_prefix
        runtime_context.tmpdir_prefix = file_store.getLocalTempDir()
        runtime_context.make_fs_access = functools.partial(
            ToilFsAccess, file_store=file_store)
        runtime_context.toil_get_file = functools.partial(
            toil_get_file, file_store, index, existing)

        process_uuid = uuid.uuid4()
        started_at = datetime.datetime.now()
        # Run the tool
        (output, status) = cwltool.executors.SingleJobExecutor().execute(
            self.cwltool, cwljob, runtime_context, cwllogger)
        ended_at = datetime.datetime.now()
        if status != "success":
            raise cwltool.errors.WorkflowException(status)

        adjustDirObjs(output, functools.partial(
            get_listing, cwltool.stdfsaccess.StdFsAccess(outdir),
            recursive=True))

        adjustFileObjs(output, functools.partial(
            uploadFile, functools.partial(writeGlobalFileWrapper, file_store),
            index, existing))

        # metadata[process_uuid] = {
        #     'started_at': started_at,
        #     'ended_at': ended_at,
        #     'job_order': cwljob,
        #     'outputs': output,
        #     'internal_name': self.jobName
        # }
        return output


def makeJob(tool, jobobj, step_inputs, runtime_context, conditional):
    """Create the correct Toil Job object for the CWL tool (workflow, job, or job
    wrapper for dynamic resource requirements.)

    """

    if tool.tool["class"] == "Workflow":
        wfjob = CWLWorkflow(tool, jobobj, runtime_context, conditional=conditional)
        followOn = ResolveIndirect(wfjob.rv())
        wfjob.addFollowOn(followOn)
        return (wfjob, followOn)
    else:
        # get_requirement
        resourceReq, _ = tool.get_requirement("ResourceRequirement")
        if resourceReq:
            for req in ("coresMin", "coresMax", "ramMin", "ramMax",
                        "tmpdirMin", "tmpdirMax", "outdirMin", "outdirMax"):
                r = resourceReq.get(req)
                if isinstance(r, string_types) and ("$(" in r or "${" in r):
                    # Found a dynamic resource requirement so use a job wrapper
                    job = CWLJobWrapper(tool, jobobj, runtime_context, conditional=conditional)
                    return (job, job)
        job = CWLJob(tool, jobobj, runtime_context, conditional=conditional)
        return (job, job)


class CWLScatter(Job):
    """Implement workflow scatter step.  When run, this creates a child job for
    each parameterization of the scatter.

    """

    def __init__(self, step, cwljob, runtime_context, conditional):
        super(CWLScatter, self).__init__()
        self.step = step
        self.cwljob = cwljob
        self.runtime_context = runtime_context
        self.conditional = conditional

    def flat_crossproduct_scatter(self,
                                  joborder,
                                  scatter_keys,
                                  outputs, postScatterEval):
        scatter_key = shortname(scatter_keys[0])
        for n in range(0, len(joborder[scatter_key])):
            jo = copy.copy(joborder)
            jo[scatter_key] = joborder[scatter_key][n]
            if len(scatter_keys) == 1:
                jo = postScatterEval(jo)
                (subjob, followOn) = makeJob(
                    self.step.embedded_tool, jo, None, self.runtime_context,
                    conditional=self.conditional)
                self.addChild(subjob)
                outputs.append(followOn.rv())
            else:
                self.flat_crossproduct_scatter(
                    jo, scatter_keys[1:], outputs, postScatterEval)

    def nested_crossproduct_scatter(self,
                                    joborder, scatter_keys, postScatterEval):
        scatter_key = shortname(scatter_keys[0])
        outputs = []
        for n in range(0, len(joborder[scatter_key])):
            jo = copy.copy(joborder)
            jo[scatter_key] = joborder[scatter_key][n]
            if len(scatter_keys) == 1:
                jo = postScatterEval(jo)
                (subjob, followOn) = makeJob(
                    self.step.embedded_tool, jo, None, self.runtime_context,
                    conditional=self.conditional)
                self.addChild(subjob)
                outputs.append(followOn.rv())
            else:
                outputs.append(self.nested_crossproduct_scatter(
                    jo, scatter_keys[1:], postScatterEval))
        return outputs

    def run(self, file_store):
        cwljob = resolve_indirect(self.cwljob)

        if isinstance(self.step.tool["scatter"], string_types):
            scatter = [self.step.tool["scatter"]]
        else:
            scatter = self.step.tool["scatter"]

        scatterMethod = self.step.tool.get("scatterMethod", None)
        if len(scatter) == 1:
            scatterMethod = "dotproduct"
        outputs = []

        valueFrom = {shortname(i["id"]): i["valueFrom"]
                     for i in self.step.tool["inputs"] if "valueFrom" in i}

        def postScatterEval(io):
            shortio = {shortname(k): v for k, v in iteritems(io)}
            for k in valueFrom:
                io.setdefault(k, None)

            def valueFromFunc(k, v):
                if k in valueFrom:
                    return cwltool.expression.do_eval(
                        valueFrom[k], shortio, self.step.requirements,
                        None, None, {}, context=v)
                else:
                    return v
            return {k: valueFromFunc(k, v) for k, v in list(io.items())}

        if scatterMethod == "dotproduct":
            for i in range(0, len(cwljob[shortname(scatter[0])])):
                copyjob = copy.copy(cwljob)
                for sc in [shortname(x) for x in scatter]:
                    copyjob[sc] = cwljob[sc][i]
                copyjob = postScatterEval(copyjob)
                (subjob, follow_on) = makeJob(
                    self.step.embedded_tool, copyjob, None,
                    self.runtime_context, conditional=self.conditional)
                self.addChild(subjob)
                outputs.append(follow_on.rv())
        elif scatterMethod == "nested_crossproduct":
            outputs = self.nested_crossproduct_scatter(
                cwljob, scatter, postScatterEval)
        elif scatterMethod == "flat_crossproduct":
            self.flat_crossproduct_scatter(
                cwljob, scatter, outputs, postScatterEval)
        else:
            if scatterMethod:
                raise validate.ValidationException(
                    "Unsupported complex scatter type '%s'" % scatterMethod)
            else:
                raise validate.ValidationException(
                    "Must provide scatterMethod to scatter over multiple"
                    " inputs.")

        return outputs


class CWLGather(Job):
    """Follows on to a scatter.  This gathers the outputs of each job in the
    scatter into an array for each output parameter.

    """

    def __init__(self, step, outputs):
        super(CWLGather, self).__init__()
        self.step = step
        self.outputs = outputs

    def allkeys(self, obj, keys):
        if isinstance(obj, Mapping):
            for k in list(obj.keys()):
                keys.add(k)
        elif isinstance(obj, MutableSequence):
            for l in obj:
                self.allkeys(l, keys)

    def extract(self, obj, k):
        if isinstance(obj, Mapping):
            return obj.get(k)
        elif isinstance(obj, MutableSequence):
            cp = []
            for l in obj:
                cp.append(self.extract(l, k))
            return cp
        else:
            return []

    def run(self, file_store):
        outobj = {}

        def sn(n):
            if isinstance(n, Mapping):
                return shortname(n["id"])
            if isinstance(n, string_types):
                return shortname(n)

        for k in [sn(i) for i in self.step.tool["out"]]:
            outobj[k] = self.extract(self.outputs, k)

        return outobj


class SelfJob(object):
    """Fake job object to facilitate implementation of CWLWorkflow.run()"""

    def __init__(self, j, v):
        self.j = j
        self.v = v

    def rv(self):
        return self.v

    def addChild(self, c):
        return self.j.addChild(c)

    def hasChild(self, c):
        return self.j.hasChild(c)


def remove_pickle_problems(obj):
    """doc_loader does not pickle correctly, causing Toil errors, remove from
       objects.
    """
    if hasattr(obj, "doc_loader"):
        obj.doc_loader = None
    if hasattr(obj, "embedded_tool"):
        obj.embedded_tool = remove_pickle_problems(obj.embedded_tool)
    if hasattr(obj, "steps"):
        obj.steps = [remove_pickle_problems(s) for s in obj.steps]
    return obj


class CWLWorkflow(Job):
    """Traverse a CWL workflow graph and create a Toil job graph with appropriate
    dependencies.

    """

    def __init__(self, cwlwf, cwljob, runtime_context, conditional=None):
        super(CWLWorkflow, self).__init__()
        self.cwlwf = cwlwf
        self.cwljob = cwljob
        self.runtime_context = runtime_context
        self.cwlwf = remove_pickle_problems(self.cwlwf)
        self.conditional = conditional or Conditional()

    def run(self, file_store):
        cwljob = resolve_indirect(self.cwljob)

        if self.conditional.is_false(cwljob):
            return self.conditional.skipped_outputs()

        # `promises` dict
        # from: each parameter (workflow input or step output)
        #   that may be used as a "source" for a step input workflow output
        #   parameter
        # to: the job that will produce that value.
        promises = {}

        # `jobs` dict from step id to job that implements that step.
        jobs = {}

        for inp in self.cwlwf.tool["inputs"]:
            promises[inp["id"]] = SelfJob(self, cwljob)

        all_outputs_fulfilled = False
        while not all_outputs_fulfilled:
            # Iteratively go over the workflow steps, scheduling jobs as their
            # dependencies can be fufilled by upstream workflow inputs or
            # step outputs. Loop exits when the workflow outputs
            # are satisfied.

            all_outputs_fulfilled = True

            for step in self.cwlwf.steps:
                if step.tool["id"] not in jobs:
                    stepinputs_fufilled = True
                    for inp in step.tool["inputs"]:
                        if "source" in inp:
                            for s in aslist(inp["source"]):
                                if s not in promises:
                                    stepinputs_fufilled = False
                    if stepinputs_fufilled:
                        jobobj = {}

                        for inp in step.tool["inputs"]:
                            key = shortname(inp["id"])
                            if "source" in inp:
                                jobobj[key] = \
                                    ResolveSource(
                                        name="%s/%s" % (step.tool["id"], key),
                                        input=inp,
                                        source_key="source",
                                        promises=promises)

                            if "default" in inp:
                                jobobj[key] = DefaultWithSource(
                                    copy.copy(inp["default"]), jobobj.get(key))

                            if "valueFrom" in inp \
                                    and "scatter" not in step.tool:
                                jobobj[key] = StepValueFrom(
                                    inp["valueFrom"], jobobj.get(key, JustAValue(None)),
                                    self.cwlwf.requirements)

                        conditional = Conditional(
                            expression=step.tool.get("when"),
                            outputs=step.tool["out"],
                            requirements=self.cwlwf.requirements)

                        if "scatter" in step.tool:
                            wfjob = CWLScatter(step, IndirectDict(jobobj),
                                               self.runtime_context,
                                               conditional=conditional)
                            followOn = CWLGather(step, wfjob.rv())
                            wfjob.addFollowOn(followOn)
                        else:
                            (wfjob, followOn) = makeJob(
                                step.embedded_tool, IndirectDict(jobobj),
                                step.tool["inputs"],
                                self.runtime_context,
                                conditional=conditional)

                        jobs[step.tool["id"]] = followOn

                        connected = False
                        for inp in step.tool["inputs"]:
                            for s in aslist(inp.get("source", [])):
                                if (isinstance(
                                        promises[s], (CWLJobWrapper, CWLGather)
                                ) and not promises[s].hasFollowOn(wfjob)):
                                    promises[s].addFollowOn(wfjob)
                                    connected = True
                                if (not isinstance(
                                        promises[s], (CWLJobWrapper, CWLGather)
                                ) and not promises[s].hasChild(wfjob)):
                                    promises[s].addChild(wfjob)
                                    connected = True
                        if not connected:
                            # the workflow step has default inputs only & isn't
                            # connected to other jobs, so add it as child of
                            # this workflow.
                            self.addChild(wfjob)

                        for out in step.tool["outputs"]:
                            promises[out["id"]] = followOn

                for inp in step.tool["inputs"]:
                    for source in aslist(inp.get("source", [])):
                        if source not in promises:
                            all_outputs_fulfilled = False

            # may need a test
            for out in self.cwlwf.tool["outputs"]:
                if "source" in out:
                    if out["source"] not in promises:
                        all_outputs_fulfilled = False

        outobj = {}
        for out in self.cwlwf.tool["outputs"]:
            key = shortname(out["id"])
            outobj[key] = (
                ResolveSource(
                    name="Workflow output '%s'" % key,
                    input=out,
                    source_key="outputSource",
                    promises=promises))

        return IndirectDict(outobj)


cwltool.process.supportedProcessRequirements = (
    "DockerRequirement", "ExpressionEngineRequirement",
    "InlineJavascriptRequirement", "InitialWorkDirRequirement",
    "SchemaDefRequirement", "EnvVarRequirement", "CreateFileRequirement",
    "SubworkflowFeatureRequirement", "ScatterFeatureRequirement",
    "ShellCommandRequirement", "MultipleInputFeatureRequirement",
    "StepInputExpressionRequirement", "ResourceRequirement")


def visitSteps(t, op):
    if isinstance(t, cwltool.workflow.Workflow):
        for s in t.steps:
            op(s.tool)
            visitSteps(s.embedded_tool, op)


def main(args=None, stdout=sys.stdout):
    """Main method for toil-cwl-runner."""
    cwllogger.removeHandler(defaultStreamHandler)
    config = Config()
    config.cwl = True
    parser = argparse.ArgumentParser()
    addOptions(parser, config)
    parser.add_argument("cwltool", type=str)
    parser.add_argument("cwljob", nargs=argparse.REMAINDER)

    # Will override the "jobStore" positional argument, enables
    # user to select jobStore or get a default from logic one below.
    parser.add_argument("--jobStore", type=str)
    parser.add_argument("--not-strict", action="store_true")
    parser.add_argument("--enable-dev", action="store_true",
                        help="Enable loading and running development versions of CWL")
    parser.add_argument("--quiet", dest="logLevel", action="store_const",
                        const="ERROR")
    parser.add_argument("--basedir", type=str)
    parser.add_argument("--outdir", type=str, default=os.getcwd())
    parser.add_argument("--version", action='version', version=baseVersion)
    dockergroup = parser.add_mutually_exclusive_group()
    dockergroup.add_argument(
        "--user-space-docker-cmd",
        help="(Linux/OS X only) Specify a user space docker command (like "
        "udocker or dx-docker) that will be used to call 'pull' and 'run'")
    dockergroup.add_argument(
        "--singularity", action="store_true", default=False,
        help="[experimental] Use Singularity runtime for running containers. "
        "Requires Singularity v2.6.1+ and Linux with kernel version v3.18+ or "
        "with overlayfs support backported.")
    dockergroup.add_argument(
        "--no-container", action="store_true", help="Do not execute jobs in a "
        "Docker container, even when `DockerRequirement` "
        "is specified under `hints`.")
    dockergroup.add_argument(
        "--leave-container", action="store_false", default=True,
        help="Do not delete Docker container used by jobs after they exit",
        dest="rm_container")

    parser.add_argument(
        "--preserve-environment", type=str, nargs='+',
        help="Preserve specified environment variables when running"
        " CommandLineTools", metavar=("VAR1 VAR2"), default=("PATH",),
        dest="preserve_environment")
    parser.add_argument(
        "--preserve-entire-environment", action="store_true",
        help="Preserve all environment variable when running "
             "CommandLineTools.",
        default=False, dest="preserve_entire_environment")
    parser.add_argument(
        "--destBucket", type=str,
        help="Specify a cloud bucket endpoint for output files.")
    parser.add_argument(
        "--beta-dependency-resolvers-configuration", default=None)
    parser.add_argument(
        "--beta-dependencies-directory", default=None)
    parser.add_argument(
        "--beta-use-biocontainers", default=None, action="store_true")
    parser.add_argument(
        "--beta-conda-dependencies", default=None, action="store_true")
    parser.add_argument(
        "--tmpdir-prefix", type=Text,
        help="Path prefix for temporary directories",
        default="tmp")
    parser.add_argument(
        "--tmp-outdir-prefix", type=Text,
        help="Path prefix for intermediate output directories",
        default="tmp")
    parser.add_argument(
        "--force-docker-pull", action="store_true", default=False,
        dest="force_docker_pull",
        help="Pull latest docker image even if it is locally present")
    parser.add_argument(
        "--no-match-user", action="store_true", default=False,
        help="Disable passing the current uid to `docker run --user`")
    parser.add_argument(
        "--no-read-only", action="store_true", default=False,
        help="Do not set root directory in the container as read-only")
    parser.add_argument(
        "--strict-memory-limit", action="store_true", help="When running with "
        "software containers and the Docker engine, pass either the "
        "calculated memory allocation from ResourceRequirements or the "
        "default of 1 gigabyte to Docker's --memory option.")
    parser.add_argument(
        "--relax-path-checks", action="store_true",
        default=False, help="Relax requirements on path names to permit "
        "spaces and hash characters.", dest="relax_path_checks")
    parser.add_argument("--default-container",
                        help="Specify a default docker container that will be "
                        "used if the workflow fails to specify one.")
    if args is None:
        args = sys.argv[1:]

    provgroup = parser.add_argument_group("Options for recording provenance "
                                          "information of the execution")
    provgroup.add_argument("--provenance",
                           help="Save provenance to specified folder as a "
                           "Research Object that captures and aggregates "
                           "workflow execution and data products.",
                           type=Text)

    provgroup.add_argument("--enable-user-provenance", default=False,
                           action="store_true",
                           help="Record user account info as part of provenance.",
                           dest="user_provenance")
    provgroup.add_argument("--disable-user-provenance", default=False,
                           action="store_false",
                           help="Do not record user account info in provenance.",
                           dest="user_provenance")
    provgroup.add_argument("--enable-host-provenance", default=False,
                           action="store_true",
                           help="Record host info as part of provenance.",
                           dest="host_provenance")
    provgroup.add_argument("--disable-host-provenance", default=False,
                           action="store_false",
                           help="Do not record host info in provenance.",
                           dest="host_provenance")
    provgroup.add_argument(
        "--orcid", help="Record user ORCID identifier as part of "
        "provenance, e.g. https://orcid.org/0000-0002-1825-0097 "
        "or 0000-0002-1825-0097. Alternatively the environment variable "
        "ORCID may be set.", dest="orcid", default=os.environ.get("ORCID", ''),
        type=Text)
    provgroup.add_argument(
        "--full-name", help="Record full name of user as part of provenance, "
        "e.g. Josiah Carberry. You may need to use shell quotes to preserve "
        "spaces. Alternatively the environment variable CWL_FULL_NAME may "
        "be set.", dest="cwl_full_name", default=os.environ.get("CWL_FULL_NAME", ''),
        type=Text)

    # Problem: we want to keep our job store somewhere auto-generated based on
    # our options, unless overridden by... an option. So we will need to parse
    # options twice, because we need to feed the parser the job store.

    # Propose a local workdir, probably under /tmp.
    # mkdtemp actually creates the directory, but
    # toil requires that the directory not exist,
    # since it is going to be our jobstore,
    # so make it and delete it and allow
    # toil to create it again (!)
    workdir = tempfile.mkdtemp()
    os.rmdir(workdir)

    # we use workdir as default default jobStore:
    options = parser.parse_args([workdir] + args)

    # if tmpdir_prefix is not the default value, set workDir if unset, and move
    # workdir and the job store under it
    if options.tmpdir_prefix != 'tmp':
        workdir = tempfile.mkdtemp(dir=options.tmpdir_prefix)
        os.rmdir(workdir)
        # Re-parse arguments with the new default jobstore under the temp dir.
        # It still might be overridden by a --jobStore option
        options = parser.parse_args([workdir] + args)
        if options.workDir is None:
            # We need to override workDir because by default Toil will pick
            # somewhere under the system temp directory if unset, ignoring
            # --tmpdir-prefix.
            #
            # If set, workDir needs to exist, so we directly use the prefix
            options.workDir = options.tmpdir_prefix

    if options.provisioner and not options.jobStore:
        raise NoSuchJobStoreException(
            'Please specify a jobstore with the --jobStore option when '
            'specifying a provisioner.')

    use_container = not options.no_container

    if options.logLevel:
        cwllogger.setLevel(options.logLevel)

    outdir = os.path.abspath(options.outdir)
    tmp_outdir_prefix = os.path.abspath(options.tmp_outdir_prefix)

    fileindex = {}
    existing = {}
    conf_file = getattr(options,
                        "beta_dependency_resolvers_configuration", None)
    use_conda_dependencies = getattr(options, "beta_conda_dependencies", None)
    job_script_provider = None
    if conf_file or use_conda_dependencies:
        dependencies_configuration = DependenciesConfiguration(options)
        job_script_provider = dependencies_configuration

    options.default_container = None
    runtime_context = cwltool.context.RuntimeContext(vars(options))
    runtime_context.find_default_container = functools.partial(
        find_default_container, options)
    runtime_context.workdir = workdir
    runtime_context.move_outputs = "leave"
    runtime_context.rm_tmpdir = False
    loading_context = cwltool.context.LoadingContext(vars(options))

    if options.provenance:
        research_obj = cwltool.provenance.ResearchObject(
            temp_prefix_ro=options.tmp_outdir_prefix, orcid=options.orcid,
            full_name=options.cwl_full_name,
            fsaccess=runtime_context.make_fs_access(''))
        runtime_context.research_obj = research_obj

    with Toil(options) as toil:
        if options.restart:
            outobj = toil.restart()
        else:
            loading_context.hints = [{
                "class": "ResourceRequirement",
                "coresMin": toil.config.defaultCores,
                "ramMin": toil.config.defaultMemory / (2**20),
                "outdirMin": toil.config.defaultDisk / (2**20),
                "tmpdirMin": 0
            }]
            loading_context.construct_tool_object = toil_make_tool
            loading_context.resolver = cwltool.resolver.tool_resolver
            loading_context.strict = not options.not_strict
            options.workflow = options.cwltool
            options.job_order = options.cwljob
            uri, tool_file_uri = cwltool.load_tool.resolve_tool_uri(
                options.cwltool, loading_context.resolver,
                loading_context.fetcher_constructor)
            options.tool_help = None
            options.debug = options.logLevel == "DEBUG"
            job_order_object, options.basedir, jobloader = \
                cwltool.main.load_job_order(
                    options, sys.stdin, loading_context.fetcher_constructor,
                    loading_context.overrides_list, tool_file_uri)

            loading_context, workflowobj, uri = cwltool.load_tool.fetch_document(uri, loading_context)
            loading_context, uri = cwltool.load_tool.resolve_and_validate_document(loading_context, workflowobj, uri)
            loading_context.overrides_list.extend(loading_context.metadata.get("cwltool:overrides", []))

            document_loader = loading_context.loader
            metadata = loading_context.metadata
            processobj = document_loader.idx

            if options.provenance and runtime_context.research_obj:
                processobj['id'] = metadata['id']
                processobj, metadata = loading_context.loader.resolve_ref(uri)
                runtime_context.research_obj.packed_workflow(
                    cwltool.main.print_pack(document_loader, processobj, uri, metadata))

            loading_context.overrides_list.extend(
                metadata.get("cwltool:overrides", []))

            try:
                tool = cwltool.load_tool.make_tool(uri, loading_context)
            except cwltool.process.UnsupportedRequirement as err:
                logging.error(err)
                return 33
            runtime_context.secret_store = SecretStore()
            initialized_job_order = cwltool.main.init_job_order(
                job_order_object, options, tool, jobloader, sys.stdout,
                secret_store=runtime_context.secret_store)
            fs_access = cwltool.stdfsaccess.StdFsAccess(options.basedir)
            fill_in_defaults(
                tool.tool["inputs"], initialized_job_order, fs_access)

            def path_to_loc(obj):
                if "location" not in obj and "path" in obj:
                    obj["location"] = obj["path"]
                    del obj["path"]

            def import_files(tool):
                visit_class(tool, ("File", "Directory"), path_to_loc)
                visit_class(tool, ("File", ), functools.partial(
                    add_sizes, fs_access))
                normalizeFilesDirs(tool)
                adjustDirObjs(tool, functools.partial(
                    get_listing, fs_access, recursive=True))
                adjustFileObjs(tool, functools.partial(
                    uploadFile, toil.importFile, fileindex, existing,
                    skip_broken=True))

            tool.visit(import_files)

            for inp in tool.tool["inputs"]:
                def set_secondary(fileobj):
                    if isinstance(fileobj, Mapping) and fileobj.get("class") == "File":
                        if "secondaryFiles" not in fileobj:
                            fileobj["secondaryFiles"] = [{"location": cwltool.builder.substitute(fileobj["location"],
                                                         sf["pattern"]), "class": "File"}
                                                         for sf in inp["secondaryFiles"]]

                    if isinstance(fileobj, MutableSequence):
                        for entry in fileobj:
                            set_secondary(entry)

                if shortname(inp["id"]) in initialized_job_order and inp.get("secondaryFiles"):
                    set_secondary(initialized_job_order[shortname(inp["id"])])

            import_files(initialized_job_order)
            visitSteps(tool, import_files)

            try:
                runtime_context.use_container = use_container
                runtime_context.tmp_outdir_prefix = os.path.realpath(
                    tmp_outdir_prefix)
                runtime_context.job_script_provider = job_script_provider
                runtime_context.force_docker_pull = options.force_docker_pull
                runtime_context.no_match_user = options.no_match_user
                runtime_context.no_read_only = options.no_read_only
                (wf1, _) = makeJob(tool, {}, None, runtime_context, conditional=None)
            except cwltool.process.UnsupportedRequirement as err:
                logging.error(err)
                return 33

            wf1.cwljob = initialized_job_order
            outobj = toil.start(wf1)

        outobj = resolve_indirect(outobj)

        # Stage files. Specify destination bucket if specified in CLI
        # options. If destination bucket not passed in,
        # options.destBucket's value will be None.
        toilStageFiles(
            toil,
            outobj,
            outdir,
            fileindex,
            existing,
            export=True,
            destBucket=options.destBucket)

        if runtime_context.research_obj is not None:
            runtime_context.research_obj.create_job(outobj, None, True)

            def remove_at_id(doc):
                if isinstance(doc, MutableMapping):
                    for key in list(doc.keys()):
                        if key == '@id':
                            del doc[key]
                        else:
                            value = doc[key]
                            if isinstance(value, MutableMapping):
                                remove_at_id(value)
                            if isinstance(value, MutableSequence):
                                for entry in value:
                                    if isinstance(value, MutableMapping):
                                        remove_at_id(entry)
            remove_at_id(outobj)
            visit_class(outobj, ("File",), functools.partial(
                add_sizes, runtime_context.make_fs_access('')))
            prov_dependencies = cwltool.main.prov_deps(workflowobj, document_loader, uri)
            runtime_context.research_obj.generate_snapshot(prov_dependencies)
            runtime_context.research_obj.close(options.provenance)

        if not options.destBucket:
            visit_class(outobj, ("File",), functools.partial(
                compute_checksums, cwltool.stdfsaccess.StdFsAccess("")))

        visit_class(outobj, ("File", ), MutationManager().unset_generation)
        stdout.write(json.dumps(outobj, indent=4))

    return 0


def find_default_container(args, builder):
    default_container = None
    if args.default_container:
        default_container = args.default_container
    elif args.beta_use_biocontainers:
        default_container = get_container_from_software_requirements(
            args, builder)

    return default_container