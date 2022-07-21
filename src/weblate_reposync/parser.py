# TODO: Allow paths to be specified without quotes.
# TODO: Disallow empty identifiers (e.g. repos, projects).

import enum

class Parser:    
    def parse(self, fname, encoding, section_cls, ctx = None):
        self._ctx = ctx
        self._line_index = 0
        self._at_eof = False
        exc = None
        
        try:
            with open(fname, mode = "rt", encoding = encoding) as self._f:
                section = self._parse_section(section_cls, False)
        except (OSError, UnicodeError) as e:
            pos_s = " at line {}".format(self._line_index) if (self._line_index > 0) else ""
            exc = e
        except _ParserError as e:
            if e.col is not None:
                pos_s = " at line {} col {}".format(self._line_index, e.col + 1) if (e.col >= 0) else " at end of line {}".format(self._line_index)
            else:
                pos_s = " at line {}".format(self._line_index)
            exc = e

        if exc:
            raise ParserError("Failed to read config ({}){}: {}".format(fname, pos_s, exc))
        
        return section
    
    @staticmethod
    def dump(section): # TODO: Move it to section class?
        Parser._dump(section, 0, False)

    @staticmethod
    def _dump(section, level, multi):
        section_cls = section.__class__

        Parser._dump_level("{} {{".format(section_cls.__name__), level if multi else 0)
    
        for (field_name, field) in section_cls.__dict__.items():
            if not isinstance(field, Field):
                continue

            field_multi = field.multi
            
            Parser._dump_level(".{} = ".format(field_name), level + 1, end = False)

            if field_multi:
                Parser._dump_level("[", 0)
            
            values = getattr(section, field_name)
            if not field_multi:
                values = [values]

            for value in values:
                field_ty = field.ty
                
                if field_ty in (Field.TY.String, Field.TY.Header):
                    # If value is not str (because of process_field), then do not quote.
                    
                    if isinstance(value, str):
                        value = "\"{}\"".format(value)
                    else:
                        value = str(value)
                        
                    Parser._dump_level(value, level + 2 if field_multi else 0)
                elif field_ty == Field.TY.Section:
                    Parser._dump(value, level + 2 if field_multi else level + 1, field_multi)
                else:
                    assert False
                    
            if field_multi:
                Parser._dump_level("]", level + 1)
                
        Parser._dump_level("}", level)

    @staticmethod
    def _dump_level(s, level, end = True):
        print("{}{}".format("   " * level, s), end = "\n" if end else "")
        
    def _parse_section(self, section_cls, in_sub):        
        # Instantiate section.

        section = section_cls()
        
        # Parse fields.

        section_cls_name = section_cls.__name__
        fields = {}
        header_field = None

        for (field_name, field) in section_cls.__dict__.items():
            if not isinstance(field, Field):
                continue

            field_ty = field.ty
            field_ident = field.ident
            field_multi = field.multi
            field_section_cls = field.section_cls
            field_have_default = field.have_default
            is_header = False

            if field_ty == Field.TY.String:
                assert field_section_cls is None, "{}.{}: section_cls is not allowed for String".format(section_cls_name, field_name)
                assert not (field_multi and field_have_default), "{}.{}: default is not allowed together with multi".format(section_cls_name, field_name)
            elif field_ty == Field.TY.Section:
                assert field_section_cls is not None, "{}.{}: section_cls is required for Section".format(section_cls_name, field_name)
                assert not field_have_default, "{}.{}: default is not allowed for Section".format(section_cls_name, field_name)
            elif field_ty == Field.TY.Header:
                assert in_sub, "{}.{}: can not have Header, as it is the main section".format(section_cls_name, field_name)
                assert not header_field, "{}.{}: Header is duplicated".format(section_cls_name, field_name)
                assert field_ident is None, "{}.{}: ident is not allowed for Header".format(section_cls_name, field_name)
                assert not field_multi, "{}.{}: multi is not allowed for Header".format(section_cls_name, field_name)
                assert field_section_cls is None, "{}.{}: section_cls is not allowed for Header".format(section_cls_name, field_name)
                assert not field_have_default, "{}.{}: default is not allowed for Header".format(section_cls_name, field_name)
                
                header_field = field
                is_header = True
            else:
                assert False, "{}.{}: ty is unknown".format(section_cls_name, field_name)
                
            field.name = field_name

            if not is_header:
                if field_ident is None:
                    field_ident = field.ident = field_name
                    
                assert field_ident not in fields, "{}.{}: ident is duplicated".format(section_cls_name, field_name)
                fields[field_ident] = field

            # Set defaults.

            value = NoValue

            if field_multi:
                value = []
            elif field_have_default:
                value = field.default

            if value != NoValue:
                setattr(section, field_name, value)

        stored = {} # Already stored field names.
            
        # Parse header.

        if in_sub:
            if header_field:
                value = self._expect(Token.TY.String)
                self._store(section, stored, header_field, value)

            self._expect(Token.TY.CurlyOpen)
            self._eol()
                
        # Parse section.

        closed = False
        
        while True:
            # Get next line of tokens.

            if not self._get_tokens(): # At EOF.
                break
            elif len(self._tokens) == 0: # Skip empty lines.
                continue

            # Process tokens.

            token = self._next()
            assert token
            token_ty = token.ty

            if token_ty == Token.TY.CurlyClose and in_sub:
                self._eol()
                closed = True
                break

            field_ident = self._expect(Token.TY.Keyword, token)
            if field_ident not in fields:
                raise _ParserError("Unknown keyword", token.start)
            
            field = fields[field_ident]
            field_ty = field.ty

            if field_ty == Field.TY.String:
                value = self._expect(Token.TY.String)
                self._eol()
            elif field_ty == Field.TY.Section:
                value = self._parse_section(field.section_cls, True)
            else:
                assert False

            self._store(section, stored, field, value)
                
        # Check for closing curly.

        if in_sub and not closed:
            raise _ParserError("Unterminated section (missing closing curly)")

        # Check if all fields are present.

        missing_field_idents = [field.ident for field in fields.values() if field.name not in section.__dict__]
        if len(missing_field_idents) > 0:
            raise _ParserError("Missing value for {}".format(", ".join(missing_field_idents)))

        return section
    
    def _get_tokens(self):
        if self._at_eof:
            return False

        self._line_index += 1

        line = self._f.readline()
        if line == "":
            self._at_eof = True
            return False

        line = line.rstrip() # Remove trailing whitespace (newline).
            
        tokenizer = Tokenizer(line)
        self._tokens = tokenizer.get_tokens()
        self._token_index = 0
        
        return True
    
    def _next(self):
        # Consume next token.
        
        if self._token_index < len(self._tokens):
            token = self._tokens[self._token_index]
            self._token_index += 1
        else:
            token = None

        return token

    def _expect(self, token_ty, token = None):
        if token is None:
            token = self._next()
            
        name = Token.ty_to_name(token_ty)
        
        if token is None:
            raise _ParserError("{} is expected".format(name), -1)
        if token.ty != token_ty:
            raise _ParserError("{} is expected".format(name), token.start)
        
        return token.value

    def _eol(self):
        token = self._next()
        
        if token is not None:
            raise _ParserError("End of line expected", token.start)

    def _store(self, section, stored, field, value):
        field_name = field.name
        field_ident = field.ident
        field_multi = field.multi
        
        if not field_multi and field_name in stored:
            raise _ParserError("{} is already defined".format(field_ident))

        if field.ty != Field.TY.Section:
            process_field = getattr(section, "process_field", None)
            
            if process_field:
                try:
                    value = process_field(field, value, self._ctx)
                except ValueError as e:
                    raise _ParserError("{}".format(e))

        if field_multi:
            l = getattr(section, field_name)
            l.append(value)
        else:
            setattr(section, field_name, value)

        stored[field_name] = True
        
class NoValue:
    pass
            
class Field:
    TY = enum.Enum("TY", ("String", "Section", "Header"))
    
    def __init__(self, ident = None, ty = None, multi = False, section_cls = None, default = NoValue):
        # Parameter validation is done Parser->_parse_section.
        
        self.ident = ident
        self.ty = ty
        self.multi = multi
        self.section_cls = section_cls

        # To be able to handle None as default value, we need a special marker (NoValue)
        # value.
        
        self.have_default = default != NoValue
        if self.have_default:
            self.default = default
            
class ParserError(Exception):
    def __init__(self, message):
        super().__init__(message)
        
class Tokenizer:
    def __init__(self, line):
        self._line = line
        self._current = 0 # Current character position.
        self._tokens = []

        while True:
            self._start = self._current # Token start position.
            ch = self._next()
            
            if ch is None: # End of line.
                break
            elif ch.isspace(): # Ignore whitespace.
                continue
            elif ch == "#": # Comment, ignore rest of the line.
                break
            elif ch == "{":
                self._add_token(Token.TY.CurlyOpen)
            elif ch == "}":
                self._add_token(Token.TY.CurlyClose)
            elif ch in ("'", "\""): # Handle quoted string.
                sep = ch
                escape = False
                end = False
                value = ""

                while True:
                    ch = self._next()
                    
                    if ch is None:
                        break
                    elif escape:
                        value += ch
                        escape = False
                    elif ch == "\\":
                        escape = True
                    elif ch == sep:
                        end = True
                        break
                    else:
                        value += ch

                if not end:
                    raise _ParserError("Unterminated quoted string", self._start)

                self._add_token(Token.TY.String, value)
            elif self._is_alpha(ch): # Handle keyword.
                while self._is_alphanum(self._peek()):
                    self._skip()
                    
                self._add_token(Token.TY.Keyword)
            else:
                raise _ParserError("Invalid character", self._start)

    def get_tokens(self):
        return self._tokens

    def _add_token(self, ty, value = None):
        if value is None:
            value = self._line[self._start:self._current]
            
        token = Token(ty, value, self._start)
        self._tokens.append(token)
        
    def _next(self):
        # Consume next character.
                    
        if not self._at_end():
            ch = self._line[self._current]
            self._current += 1
        else:
            ch = None

        return ch

    def _peek(self):
        # Take a look at next character, but do not consume it.

        return self._line[self._current] if not self._at_end() else None

    def _skip(self):
        assert not self._at_end()
        self._current += 1

    def _at_end(self):
        return self._current >= len(self._line)

    def _is_alpha(self, ch):
        return ch is not None and ((ch >= "a" and ch <= "z") or (ch >= "A" and ch <= "Z") or ch == "_")

    def _is_digit(self, ch):
        return ch is not None and (ch >= "0" and ch <= "9")

    def _is_alphanum(self, ch):
        return self._is_alpha(ch) or self._is_digit(ch)

class Token:
    TY = enum.Enum("TY", ("CurlyOpen", "CurlyClose", "String", "Keyword"))

    def __init__(self, ty, value, start):
        self.ty = ty
        self.value = value
        self.start = start

    @staticmethod
    def ty_to_name(ty):
        if ty == Token.TY.CurlyOpen:
            name = "Opening curly"
        elif ty == Token.TY.CurlyClose:
            name = "Closing curly"
        elif ty == Token.TY.String:
            name = "String"
        elif ty == Token.TY.Keyword:
            name = "Keyword"
        else:
            assert False

        return name
        
class _ParserError(Exception):
    def __init__(self, message, col = None):
        super().__init__(message)
        
        self.col = col
