# Release process for Permifrost 


## When to release

### Pre-requirements

Check list:
* [ ] Alter [VERSION](https://gitlab.com/gitlab-data/permifrost/-/blob/master/VERSION) file
* [ ] Alter [CHANGELOG.md](https://gitlab.com/gitlab-data/permifrost/-/blob/master/CHANGELOG.md) file

## Who to release

## Version naming

Using the [Semantic Versioning 2.0.](https://semver.org/)


## How to release


### Release diagram

``` mermaid
flowchart TD
    Start((Start))
    END((End))
    Start --> ISSUE
    Maintainers --> VERSION
    CHANGELOG --> PYPI

    PYPI-->CLOSE
    CLOSE --> END
    
    subgraph Approvals
        ISSUE[Issue created] --> Maintainers[maintainers x3] 
        CLOSE[Closing the issue]
    end
    
    subgraph Prerequirements
        VERSION-->CHANGELOG[CHANGELOG.md] 
    end
    
    subgraph Deployment
        PYPI[PyPi publishing] 
    end
```