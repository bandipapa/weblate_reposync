# TODO: Support webhook mode.
# TODO: Add support for email notification on error.

import argparse
import copy
import fcntl
import git
import importlib
import importlib.metadata
import json
import os
import re
import sys
import urllib.error
import urllib.request

from sicopa import Parser, Field, ParserError

class Const:
    ENCODING = "utf-8"
    JSON_MIMETYPE = "application/json"
    LOCK = ".lock"

class Main:
    def start(self):
        exitcode = 0

        try:
            self._parse_args()
            self._parse_config()

            if self._dump_config:
                Parser.dump(self._config)
            else:
                self._main()
        except FatalError as e:
            print("{}: {}".format(sys.argv[0], e), file = sys.stderr)
            exitcode = 1

        sys.exit(exitcode)

    def _parse_args(self):
        # Parse command-line args.

        metadata = importlib.metadata

        parser = argparse.ArgumentParser(description = metadata.metadata("weblate_reposync")["Summary"])

        parser.add_argument("-c", metavar = "CONFIG_FILE", action = "store", required = True, dest = "config_fname",
                            help = "configuration file")
        parser.add_argument("-d", action = "store_true", dest = "dump_config",
                            help = "dump configuration (for debug purposes)")
        parser.add_argument("-n", action = "store_true", dest = "dry_run",
                            help = "dry run (will update local repositories, but don't touch weblate, implies -v)")
        parser.add_argument("-v", action = "store_true", dest = "verbose",
                            help = "turn on verbose output")
        parser.add_argument("-V", action = "version", version = metadata.version("weblate_reposync"),
                            help = "display version")

        args = parser.parse_args()

        self._config_fname = args.config_fname
        self._dump_config = args.dump_config
        self._dry_run = args.dry_run
        self._verbose = True if self._dry_run else args.verbose

    def _parse_config(self):
        # Parse config.

        parser = Parser()
        ctx = {
            "repos": {},
            "projects": {}
        }
        
        try:
            self._config = parser.parse(self._config_fname, Const.ENCODING, Config, ctx)
        except ParserError as e:
            raise FatalError("{}".format(e))

    def _main(self):
        fname = os.path.join(self._config.repo_root, Const.LOCK)

        try:
            f = open(fname, mode = "ab")
            fcntl.flock(f, fcntl.LOCK_EX)
        except OSError as e:
            raise FatalError("Failed to create lockfile ({}): {}".format(fname, e))

        # We are not using context (with/as) to open lockfile, because if any exception
        # (e.g. OSError) happens during _main_impl, we want to bubble it up.
        
        try:
            self._main_impl()
        finally:
            try:
                os.unlink(fname)
            except OSError as e:
                pass
            
    def _main_impl(self):
        # Loop over projects.

        for project in self._config.projects:
            project_slug = project.slug
            
            self._log("Processing project {}".format(project_slug))

            # Collect components based on repo content.

            component_slugs = {}
            componentinsts = []
            
            for component in project.components:
                try:
                    self._collect(project_slug, component, component_slugs, componentinsts)
                except git.exc.GitError as e:
                    raise FatalError("git: {}".format(e))

            # Merge with weblate components.

            self._merge(project_slug, componentinsts)
                
    def _collect(self, project_slug, component, component_slugs, componentinsts):
        repo = component.repo

        # A single repo can be referenced from more than one component, so
        # updating is needed only once.
        # TODO: Support other vcs, not just git.

        git_repo = repo.git_repo

        if not git_repo:
            name = repo.name
        
            self._log("   Updating repository {}".format(name))

            path = os.path.join(self._config.repo_root, name)

            if os.path.exists(path):
                git_repo = git.Repo(path)
                remote = git_repo.remote()
                remote.fetch()
            else:
                git_repo = git.Repo.clone_from(repo.url, path, multi_options = ["--bare", "--depth 1", "--no-single-branch", "--config remote.origin.fetch=+refs/heads/*:refs/remotes/origin/*"])
        
            repo.git_repo = git_repo

        # Loop over branches: origin/ is removed from branch names.

        remote = git_repo.remote()        

        branches = {branch.remote_head: branch for branch in remote.refs}
        branch_names = list(branches.keys())

        branch_filter = component.branch_filter
        if branch_filter:
            try:
                branch_names = branch_filter(branch_names)
            except Exception as e:
                raise FatalError("During calling of branch_filter: {}".format(e))

        branch_re = component.branch_re
        if branch_re:
            branch_names = [branch_name for branch_name in branch_names if branch_re.search(branch_name)]

        content_filter = component.content_filter
            
        for branch_name in branch_names:
            branch = branches[branch_name]
            commit = branch.commit

            # Iterate over files.
                    
            self._log("   Scanning repository {} branch {} (commit {})".format(repo.name, branch_name, commit.hexsha))

            for item in commit.tree.traverse():
                if item.type != "blob":
                    continue
                
                path = item.path

                r = component.path_re.search(path)
                if not r:
                    continue

                if content_filter:
                    buf = item.data_stream.read()

                    try:
                        if not content_filter(path, buf):
                            continue
                    except Exception as e:
                        raise FatalError("During calling of content_filter: {}".format(e))

                # Path matches, create component instance.
                
                substs = {key: value for (key, value) in r.groupdict().items() if value is not None}
                substs["project"] = project_slug
                substs["vcs"] = "git"
                substs["repo_url"] = repo.url
                substs["branch"] = branch_name
                substs["path"] = path
                substs["prefix"] = self._config.component_slug_prefix

                component_slug = "{}{}".format(self._config.component_slug_prefix, self._subst(component.slug, substs))

                self._log("      Component {} (path {})".format(component_slug, path))

                # TODO: Option to ignore duplicates? (This removes the need for negative (?!.../) re lookups).
                
                if component_slug in component_slugs:
                    raise FatalError("Component {} is duplicated".format(component_slug))

                # Prepare weblate component config.
                # TODO: Fill based on settings: vcs, repo, branch, push don't include them in settings_json?                

                wl_component = copy.deepcopy(component.settings_json)

                for (key, value) in list(wl_component.items()): # Just to be safe: use list() to clone keys while iterating and updating. # TODO: Is this needed?
                    if isinstance(value, str):
                        wl_component[key] = self._subst(value, substs)

                wl_component["slug"] = component_slug
                
                component_slugs[component_slug] = True

                componentinst = ComponentInst(component, wl_component)
                componentinsts.append(componentinst)
                
    def _merge(self, project_slug, componentinsts):
        self._log("   Retrieving weblate components")

        ex_wl_components = self._wl_get_components(project_slug)

        self._log("   Updating weblate components")

        for componentinst in componentinsts:
            wl_component = componentinst.wl_component
            component_slug = wl_component["slug"]

            if component_slug in ex_wl_components:
                # TODO: Patch existing components.
                # TODO: Support update addons.
                
                del ex_wl_components[component_slug]
            else:
                self._log("      Creating component {}".format(component_slug))

                if not self._dry_run:
                    self._wl_create_component(project_slug, wl_component)

                for addon in componentinst.component.addons:
                    self._log("         Creating addon {}".format(addon.name))
                    
                    if not self._dry_run:
                        self._wl_create_addon(project_slug, component_slug, addon)

        for component_slug in ex_wl_components.keys():
            self._log("      Deleting component {}".format(component_slug))

            if not self._dry_run:
                self._wl_delete_component(project_slug, component_slug)

    def _subst(self, s, substs):
        # TODO: Look into files to do substitution (e.g. UI5 manifest.json)?
        # TODO: Support for transforming substs (e.g. char replacement, {dir1}-{dir2}).        
        
        for (key, value) in substs.items():
            s = s.replace("{{{}}}".format(key), value)

        return s
    
    def _wl_get_components(self, project_slug):
        # See https://docs.weblate.org/en/latest/api.html#get--api-projects-(string-project)-components-
        # TODO: What if components are created/deleted during paging?

        url = "{}/api/projects/{}/components/".format(self._config.wl_url, project_slug)
        wl_components = {}

        while url:
            r = self._wl_request(url, need_result = True)

            for wl_component in r["results"]:
                component_slug = wl_component["slug"]

                if not component_slug.startswith(self._config.component_slug_prefix):
                    continue

                if component_slug in wl_components:
                    raise FatalError("weblate: Duplicated component {}".format(component_slug))
                
                wl_components[component_slug] = wl_component
            
            url = r["next"]

        return wl_components

    def _wl_create_component(self, project_slug, wl_component):
        # See https://docs.weblate.org/en/latest/api.html#post--api-projects-(string-project)-components-

        url = "{}/api/projects/{}/components/".format(self._config.wl_url, project_slug)        
        self._wl_request(url, method = "POST", payload = wl_component)

    def _wl_delete_component(self, project_slug, component_slug):
        # See https://docs.weblate.org/en/latest/api.html#delete--api-components-(string-project)-(string-component)-

        url = "{}/api/components/{}/{}/".format(self._config.wl_url, project_slug, component_slug)
        self._wl_request(url, method = "DELETE")

    def _wl_create_addon(self, project_slug, component_slug, addon):
        # See https://docs.weblate.org/en/latest/api.html#post--api-components-(string-project)-(string-component)-addons-

        url = "{}/api/components/{}/{}/addons/".format(self._config.wl_url, project_slug, component_slug)
        payload = {
            "name": addon.name,
            "configuration": addon.settings_json
        }
        self._wl_request(url, method = "POST", payload = payload)
        
    def _wl_request(self, url, method = "GET", payload = None, need_result = False):
        req = urllib.request.Request(url, data = json.dumps(payload).encode("utf-8") if payload else None, method = method)
        
        req.add_header("Authorization", "Token {}".format(self._config.wl_token))
        req.add_header("Accept", Const.JSON_MIMETYPE)
        
        if payload:
            req.add_header("Content-Type", Const.JSON_MIMETYPE)

        exc = None
        detail = None

        try:
            with urllib.request.urlopen(req) as resp:
                r = self._decode_json(resp) if need_result else None
        except ValueError as e:
            exc = e
        except urllib.error.HTTPError as e:
            try:
                eobj = self._decode_json(e)

                if "detail" in eobj:
                    detail = eobj["detail"]
            except ValueError:
                pass

            exc = e
        except urllib.error.URLError as e:
            exc = e

        if exc:
            raise FatalError("weblate: {} {}: {}{}".format(method, url, exc, " (detail: {})".format(detail) if detail else ""))

        return r

    def _decode_json(self, f):
        return Util.parse_json(f.read())
        
    def _log(self, message):
        if self._verbose:
            print(message)
            
class Addon:
    name = Field(ty = Field.TY.Header, non_empty = True) # TODO: Check for uniqueness or can we create the same addon multiple times?
    settings_json = Field(ty = Field.TY.String, default = {})

    def process_field(self, field, value, ctx):
        if field.name == "settings_json":
            value = Util.parse_json(value)
            
        return value
    
class Component:
    repo = Field(ident = "repository", ty = Field.TY.String)
    branch_filter = Field(ty = Field.TY.String, default = None)
    branch_re = Field(ty = Field.TY.String, default = None)
    path_re = Field(ty = Field.TY.String)
    content_filter = Field(ty = Field.TY.String, default = None)
    slug = Field(ty = Field.TY.String, non_empty = True)
    settings_json = Field(ty = Field.TY.String, default = {})
    addons = Field(ident = "addon", ty = Field.TY.Section, section_cls = Addon, multi = True)
    
    def process_field(self, field, value, ctx):
        field_name = field.name
        
        if field_name == "repo":
            repos = ctx["repos"]
            if value not in repos:
                raise ValueError("Repository {} is not defined".format(value))

            value = repos[value]
        elif field_name in ("branch_filter", "content_filter"):
            comps = value.split(":")
            if len(comps) != 2:
                raise ValueError("Filter syntax is path.to.module:filter_func")

            try:
                mod = importlib.import_module(comps[0])
            except Exception as e:
                raise ValueError("Failed to import module: {}".format(e))

            value = getattr(mod, comps[1], None)
            if value is None:
                raise ValueError("Failed to resolve name {}".format(comps[1]))
        elif field_name in ("branch_re", "path_re"):
            try:
                value = re.compile(value)
            except re.error as e:
                raise ValueError("Invalid regular expression: {}".format(e))
        elif field_name == "settings_json":
            value = Util.parse_json(value)

            if "slug" in value:
                raise ValueError("slug should not be specified")
            
        return value

class Project:
    slug = Field(ty = Field.TY.Header, non_empty = True)
    components = Field(ident = "component", ty = Field.TY.Section, section_cls = Component, multi = True)

    def process_field(self, field, value, ctx):
        if field.name == "slug":
            projects = ctx["projects"]
            if value in projects:
                raise ValueError("Project {} is already defined".format(value))
            
            projects[value] = True
            
        return value
    
class Repository:
    name = Field(ty = Field.TY.Header, non_empty = True)
    url = Field(ty = Field.TY.String, non_empty = True)

    def __init__(self):
        self.git_repo = None

    def process_field(self, field, value, ctx):
        if field.name == "name":
            repos = ctx["repos"]
            if value in repos:
                raise ValueError("Repository {} is already defined".format(value))
            
            repos[value] = self
            
        return value
        
class Config:
    repo_root = Field(ident = "repository_root", ty = Field.TY.String, non_empty = True)
    wl_url = Field(ident = "weblate_url", ty = Field.TY.String, non_empty = True)
    wl_token = Field(ident = "weblate_token", ty = Field.TY.String, non_empty = True)
    component_slug_prefix = Field(ty = Field.TY.String, default = "auto-") # TODO: Add support for suffix.
    import_path = Field(ty = Field.TY.String, non_empty = True, default = None)
    repos = Field(ident = "repository", ty = Field.TY.Section, section_cls = Repository, multi = True)
    projects = Field(ident = "project", ty = Field.TY.Section, section_cls = Project, multi = True)

    def process_field(self, field, value, ctx):
        if field.name == "import_path":
            # Adjust import path early.
            sys.path.append(value)
            
        return value

class ComponentInst:
    def __init__(self, component, wl_component):
        self.component = component
        self.wl_component = wl_component

class Util:
    @staticmethod
    def parse_json(json_s):
        try:
            value = json.loads(json_s)
        except json.decoder.JSONDecodeError as e:
            raise ValueError("Invalid JSON: {}".format(e))

        if not isinstance(value, dict):
            raise ValueError("JSON object ({ ... }) should be at top level")
        
        return value
    
class FatalError(Exception):
    def __init__(self, message):
        super().__init__(message)
        
def start():
    main = Main()
    main.start()

if __name__ == "__main__":
    start()
